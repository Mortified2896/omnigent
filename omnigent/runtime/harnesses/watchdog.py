"""Liveness-aware watchdog support for harness-supervised subprocesses.

The scaffold's per-turn watchdog (`omnigent/runtime/harnesses/_scaffold.py`)
trips when ``run_turn`` emits nothing for the configured idle window. A
harness that delegates to a supervised subprocess (e.g. a long-running
build, test, or LLM CLI) can stay quiet for minutes at a time while the
subprocess is doing real work. The watchdog cannot tell that subprocess
is alive from the harness's emit rate alone, so it would falsely kill
the turn.

This module owns the per-subprocess liveness tracker:

- :class:`SupervisedSubprocess` — registered by the harness whenever it
  spawns a long-lived child via :func:`register_supervised_subprocess`.
  Carries the child ``psutil.Process`` (or a PID-only fallback when
  psutil is unavailable), the command, and byte counters that the
  harness updates as it consumes stdout / stderr.
- :class:`LivenessTracker` — singleton per process. The scaffold's
  per-turn liveness loop iterates registered subprocesses and emits a
  :class:`omnigent.server.schemas.SubprocessLivenessEvent` for each
  one. The event payload doubles as proof-of-life (the idle watchdog
  resets on it) and diagnostics (PID, elapsed, byte counts, state,
  optional CPU / memory).
- :func:`classify_timeout` — turns a watchdog trip into a structured
  reason the scaffold can include in the timeout error. Distinguishes:

    * ``slow_but_alive`` — a registered subprocess is alive but no
      progress events have arrived within the window.
    * ``interactive_wait`` — the subprocess is alive and has unread
      stdout matching a password / sudo / pager / confirmation prompt.
    * ``dead_process`` — the registered subprocess exited or the PID
      is no longer reachable.
    * ``provider_stream_stall`` — no registered subprocess; the model
      stream itself stopped emitting.
    * ``forwarder_disconnect`` — recent native-forwarder POST failures
      recorded by :mod:`omnigent._native_forwarder_health`.
    * ``absolute_timeout`` — the total-turn cap fired.
    * ``idle_timeout`` — the idle cap fired with no further evidence.

  The scaffold always wraps a real timeout; the classification is
  operator-facing diagnostics, never an excuse to skip the failure
  surface.

Process-tree cleanup:

:func:`terminate_subprocess_tree` walks ``psutil.Process.children``
recursively and SIGTERMs each, escalating to SIGKILL after a grace
window. The scaffold calls this on a real timeout so a wedged
subprocess doesn't outlive the turn and leak across sessions.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import signal
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING

_logger = logging.getLogger(__name__)

# Probe import for psutil — the package is a hard dep but defensive
# import keeps the module importable when running under a slimmer
# minimal-runtime image that hasn't installed it yet.
try:
    import psutil  # type: ignore[import-not-found]

    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in slim images
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False

if TYPE_CHECKING:
    import psutil as _psutil_typing


# Heuristic markers that a supervised subprocess is waiting on a
# password, sudo, pager, or confirmation prompt. The watchdog treats
# these as "alive but blocked on the user" rather than "wedged".
_INTERACTIVE_PROMPT_MARKERS: tuple[str, ...] = (
    "[sudo] password",
    "Password:",
    "password for ",
    "Are you sure",
    "Do you want to continue",
    "(Y/N)",
    "[Y/n]",
    "[y/N]",
    "Press ENTER",
    "Press enter",
    "more?",
    "--More--",
    "--more--",
    "END--",
)


def _looks_like_interactive_prompt(text: str) -> str | None:
    """Return a short hint if *text* matches a known interactive prompt.

    :param text: Tail of stdout/stderr captured by the harness.
    :returns: The matching marker substring (trimmed), or ``None`` when
        no known prompt is present in the captured text.
    """
    if not text:
        return None
    lowered = text.lower()
    for marker in _INTERACTIVE_PROMPT_MARKERS:
        if marker.lower() in lowered:
            return marker.strip()
    return None


class TimeoutReason(str, enum.Enum):
    """Operator-facing classification of a watchdog trip."""

    IDLE_TIMEOUT = "idle_timeout"
    ABSOLUTE_TIMEOUT = "absolute_timeout"
    MAX_TOOL_RUNTIME = "max_tool_runtime"
    SLOW_BUT_ALIVE = "slow_but_alive"
    INTERACTIVE_WAIT = "interactive_wait"
    DEAD_PROCESS = "dead_process"
    PROVIDER_STREAM_STALL = "provider_stream_stall"
    FORWARDER_DISCONNECT = "forwarder_disconnect"


@dataclass
class SupervisedSubprocess:
    """A registered subprocess whose liveness the watchdog tracks.

    The harness instantiates one of these per supervised child and
    registers it with :func:`register_supervised_subprocess` at
    spawn time. The watchdog loop reads the byte counters and
    prompt hint, computes state, and emits a
    :class:`SubprocessLivenessEvent` while the process is alive.

    :param name: Human-readable identifier the harness picks, e.g.
        ``"claude-cli"`` or ``"pytest"``. Carried verbatim into
        ``SubprocessLivenessEvent.command``.
    :param pid: Process id of the supervised child. ``None`` after
        the process has exited (the tracker keeps the entry around
        so the scaffold can report "exited mid-turn" in the timeout
        error message).
    :param command: Full argv the harness launched with, stored for
        diagnostics. The watchdog truncates to the first 256
        characters when emitting the SSE event.
    :param started_at: Unix epoch seconds when the harness registered
        the subprocess. Used to compute ``elapsed_s``.
    :param stdout_bytes: Cumulative byte count the harness has read
        from the subprocess's stdout. Updated by the harness via
        :attr:`byte_counters` after each read.
    :param stderr_bytes: Same as :attr:`stdout_bytes` for stderr.
    :param last_stdout_at: ISO-8601 wall-clock timestamp of the
        most recent stdout read. ``None`` until the harness writes
        one.
    :param last_stderr_at: Same as :attr:`last_stdout_at` for stderr.
    :param interactive_hint: Last matched interactive-prompt marker,
        or ``None``. The harness sets this from its own prompt
        detection (or from :func:`_looks_like_interactive_prompt`
        applied to its stdout tail).
    """

    name: str
    pid: int | None
    command: str
    started_at: float
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    last_stdout_at: str | None = None
    last_stderr_at: str | None = None
    interactive_hint: str | None = None


@dataclass
class _TrackedState:
    """Singleton state for the liveness tracker."""

    lock: RLock = field(default_factory=RLock)
    by_pid: dict[int, SupervisedSubprocess] = field(default_factory=dict)


_STATE = _TrackedState()


def register_supervised_subprocess(
    *,
    name: str,
    pid: int,
    command: Sequence[str] | str,
    started_at: float | None = None,
) -> SupervisedSubprocess:
    """Register a supervised child with the liveness tracker.

    The scaffold's liveness loop will start emitting
    :class:`SubprocessLivenessEvent`s for this process until
    :func:`unregister_supervised_subprocess` is called or the
    tracker observes the process has exited.

    :param name: Human-readable identifier the harness picks.
    :param pid: Process id of the supervised child.
    :param command: Argv (or a single command string) the harness
        launched with. Stored verbatim for diagnostics.
    :param started_at: Unix epoch seconds the harness considers the
        subprocess start time. Defaults to "now".
    :returns: The registered :class:`SupervisedSubprocess` so the
        harness can update its byte counters as it reads stdout /
        stderr.
    """
    if isinstance(command, str):
        command_str = command
    else:
        command_str = " ".join(str(part) for part in command)
    entry = SupervisedSubprocess(
        name=name,
        pid=pid,
        command=command_str,
        started_at=started_at if started_at is not None else time.time(),
    )
    with _STATE.lock:
        _STATE.by_pid[pid] = entry
    return entry


def unregister_supervised_subprocess(pid: int) -> None:
    """Remove a previously-registered subprocess from the tracker.

    Idempotent — calling on an unknown PID is a no-op. The harness
    should call this from its normal subprocess-exit handler so
    the tracker doesn't keep emitting liveness events for a child
    that has long since been reaped.

    :param pid: Process id passed to
        :func:`register_supervised_subprocess` at spawn time.
    """
    with _STATE.lock:
        _STATE.by_pid.pop(pid, None)


def tracked_subprocesses() -> list[SupervisedSubprocess]:
    """Return a snapshot of every currently-tracked subprocess.

    :returns: A list copy of the registered entries; the caller may
        mutate it freely. The internal state is unaffected.
    """
    with _STATE.lock:
        return list(_STATE.by_pid.values())


def update_subprocess_io(
    pid: int,
    *,
    stdout_bytes: int | None = None,
    stderr_bytes: int | None = None,
    stdout_tail: str | None = None,
    stderr_tail: str | None = None,
    interactive_hint: str | None = None,
) -> None:
    """Record new I/O activity from the harness's read loop.

    The harness calls this from whatever coroutine is draining the
    subprocess's stdout / stderr pipes. The watchdog reads the
    counters and prompt hints to compute ``state`` for the next
    liveness event.

    :param pid: Process id passed to
        :func:`register_supervised_subprocess`.
    :param stdout_bytes: New cumulative stdout byte count. ``None``
        leaves the existing value intact.
    :param stderr_bytes: Same for stderr.
    :param stdout_tail: Most recent stdout text. The watchdog
        applies :func:`_looks_like_interactive_prompt` when the
        harness doesn't supply ``interactive_hint`` explicitly.
    :param stderr_tail: Same for stderr.
    :param interactive_hint: Explicit override for the prompt hint,
        e.g. when the harness has its own detection. ``None`` means
        "infer from the tails if possible".
    """
    with _STATE.lock:
        entry = _STATE.by_pid.get(pid)
    if entry is None:
        return
    if stdout_bytes is not None:
        entry.stdout_bytes = stdout_bytes
    if stderr_bytes is not None:
        entry.stderr_bytes = stderr_bytes
    now_iso = _utc_now_iso()
    if stdout_tail is not None:
        entry.last_stdout_at = now_iso
    if stderr_tail is not None:
        entry.last_stderr_at = now_iso
    if interactive_hint is not None:
        entry.interactive_hint = interactive_hint
    elif stdout_tail is not None or stderr_tail is not None:
        hint = _looks_like_interactive_prompt(stdout_tail or "") or _looks_like_interactive_prompt(
            stderr_tail or ""
        )
        entry.interactive_hint = hint


def reset_for_tests() -> None:  # pragma: no cover - test helper
    """Clear the tracker state. Intended for unit-test isolation."""
    with _STATE.lock:
        _STATE.by_pid.clear()


def _utc_now_iso() -> str:
    """Return the current UTC time in ISO-8601 form (e.g. ``...Z``)."""
    import datetime as _dt

    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _psutil_process(pid: int) -> _psutil_typing.Process | None:
    """Return a ``psutil.Process`` for *pid* or ``None`` if unavailable."""
    if not _PSUTIL_AVAILABLE or pid is None:
        return None
    try:
        return psutil.Process(pid)  # type: ignore[union-attr]
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        return None


def _read_proc_state(
    entry: SupervisedSubprocess,
) -> tuple[str, str | None, float | None, int | None]:
    """Inspect a tracked subprocess and return (state, hint, cpu%, rss).

    The function never raises — a missing ``psutil.Process`` is
    treated as ``dead_process`` so the watchdog can surface the
    exit rather than silently lose the supervision.

    :param entry: Tracked subprocess entry to inspect.
    :returns: ``(state, interactive_hint, cpu_percent, rss_bytes)``.
    """
    proc = _psutil_process(entry.pid) if entry.pid is not None else None
    if proc is None:
        return ("dead_process", None, None, None)
    try:
        status = proc.status()
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        return ("dead_process", None, None, None)
    if status == psutil.STATUS_ZOMBIE:  # type: ignore[union-attr]
        return ("zombie", None, None, None)
    cpu_percent: float | None = None
    rss_bytes: int | None = None
    try:
        with proc.oneshot():
            cpu_percent = float(proc.cpu_percent(interval=None))
            memory_info = proc.memory_info()
            rss_bytes = int(getattr(memory_info, "rss", 0) or 0)
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        cpu_percent = None
        rss_bytes = None
    if entry.interactive_hint:
        return ("interactive_wait", entry.interactive_hint, cpu_percent, rss_bytes)
    return ("alive", None, cpu_percent, rss_bytes)


def build_liveness_event(
    entry: SupervisedSubprocess, *, now: float | None = None
) -> SubprocessLivenessEventPayload:
    """Snapshot *entry* into a liveness event payload (no I/O).

    Pure helper — the scaffold's liveness loop calls this once per
    registered subprocess per heartbeat tick.

    :param entry: Tracked subprocess entry.
    :param now: Override for the elapsed-time anchor. ``None``
        defaults to :func:`time.time`.
    :returns: A plain dict matching :class:`SubprocessLivenessEvent`
        so the scaffold can construct the SSE event without
        importing pydantic directly from inside the watchdog loop.
    """
    state, hint, cpu_percent, rss_bytes = _read_proc_state(entry)
    elapsed = (now if now is not None else time.time()) - entry.started_at
    return {
        "type": "response.subprocess_live",
        "command": entry.command[:256],
        "pid": entry.pid,
        "elapsed_s": elapsed,
        "last_stdout_at": entry.last_stdout_at,
        "last_stderr_at": entry.last_stderr_at,
        "stdout_bytes": entry.stdout_bytes,
        "stderr_bytes": entry.stderr_bytes,
        "state": state,
        "cpu_percent": cpu_percent,
        "rss_bytes": rss_bytes,
        "interactive_hint": hint,
    }


# Type alias for clarity (the dict shape mirrors the SSE event).
SubprocessLivenessEventPayload = dict[str, object]


def classify_timeout(
    *,
    trip_kind: str,
    has_tracked_subprocess: bool,
    last_subprocess_state: str | None,
    last_subprocess_pid: int | None,
    last_subprocess_alive: bool,
    forwarder_failure: object | None,
) -> TimeoutReason:
    """Map a watchdog trip to a structured reason.

    The scaffold passes a snapshot of "what we know at the moment of
    trip" and gets back a :class:`TimeoutReason`. The mapping is
    intentionally conservative — when multiple signals are present
    the scaffold surfaces them all in the timeout error message,
    never picking one over another.

    :param trip_kind: Either ``"idle"`` (the per-turn idle deadline
        fired) or ``"absolute"`` (the total-turn cap fired).
    :param has_tracked_subprocess: ``True`` if any
        :class:`SupervisedSubprocess` was registered when the
        trip fired.
    :param last_subprocess_state: ``state`` field of the most
        recent liveness event (``alive`` /
        ``interactive_wait`` / ``dead_process`` / ``zombie``), or
        ``None`` if no event was emitted before the trip.
    :param last_subprocess_pid: PID of that subprocess.
    :param last_subprocess_alive: ``True`` when the most recent
        liveness event had ``state in {"alive", "interactive_wait"}``.
    :param forwarder_failure: Result of
        :func:`omnigent._native_forwarder_health.recent_post_failure`
        — ``None`` when no recent failure was recorded.
    :returns: The structured reason for the timeout.
    """
    if trip_kind == "absolute":
        return TimeoutReason.ABSOLUTE_TIMEOUT
    if forwarder_failure is not None:
        return TimeoutReason.FORWARDER_DISCONNECT
    if has_tracked_subprocess and last_subprocess_state == "dead_process":
        return TimeoutReason.DEAD_PROCESS
    if (
        has_tracked_subprocess
        and last_subprocess_state == "interactive_wait"
        and last_subprocess_alive
    ):
        return TimeoutReason.INTERACTIVE_WAIT
    if has_tracked_subprocess and last_subprocess_alive:
        return TimeoutReason.SLOW_BUT_ALIVE
    return TimeoutReason.PROVIDER_STREAM_STALL


def terminate_subprocess_tree(
    pid: int,
    *,
    grace_s: float = 4.0,
    escalate_to_sigkill: bool = True,
    on_event: Callable[[str, dict[str, object]], None] | None = None,
) -> None:
    """Walk the child tree of *pid* and terminate it cleanly.

    Used by the scaffold on a real watchdog timeout. Walks
    ``psutil.Process.children(recursive=True)`` and ``SIGTERM``s
    each, escalating to ``SIGKILL`` after *grace_s* when
    *escalate_to_sigkill* is true. The original process is
    terminated last so its children have a chance to flush.

    Never raises — the scaffold needs a clean timeout surface even
    when psutil can't read /proc on a misbehaving container.

    :param pid: Process id whose tree should be terminated.
    :param grace_s: Seconds between SIGTERM and SIGKILL.
    :param escalate_to_sigkill: ``False`` leaves SIGTERM sent and
        skips the kill escalation (useful in tests).
    :param on_event: Optional callable invoked with a structured
        event name + dict for every termination step (used by the
        scaffold to wire the cleanup events into its SSE stream).
    """
    if pid is None:
        return
    if not _PSUTIL_AVAILABLE:
        _safe_kill(pid, signal.SIGTERM)
        if on_event is not None:
            on_event(
                "subprocess_tree_cleanup_skipped",
                {"pid": pid, "reason": "psutil unavailable"},
            )
        return
    try:
        root = psutil.Process(pid)  # type: ignore[union-attr]
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        if on_event is not None:
            on_event("subprocess_tree_cleanup_no_proc", {"pid": pid})
        return
    children: list[psutil.Process] = []  # type: ignore[type-defined]
    try:
        children = list(root.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        children = []
    terminated: list[int] = []
    for child in children:
        try:
            child.terminate()
            terminated.append(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
            continue
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        root.terminate()
    if on_event is not None:
        on_event(
            "subprocess_tree_terminated",
            {
                "pid": pid,
                "children": [c.pid for c in children],
                "grace_s": grace_s,
            },
        )
    if not escalate_to_sigkill or grace_s <= 0:
        return
    deadline = time.time() + grace_s
    live = list(children)
    try:
        root_alive = root.wait(timeout=grace_s) is None
    except (psutil.TimeoutExpired, psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        root_alive = True
    live = [c for c in live if c.is_running()]
    if root_alive:
        try:
            root.kill()
            live.append(root)
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
            pass
    for c in live:
        try:
            c.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
            continue
    _ = deadline  # keep the deadline visible for linters / future hooks


def _safe_kill(pid: int, sig: int) -> None:
    """Send *sig* to *pid*; ignore missing / permission errors."""
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


# ---------------------------------------------------------------------------
# Idempotent-retry policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryDecision:
    """Outcome of :func:`should_retry_timeout`."""

    retry: bool
    reason: str


# Default list of tool names the watchdog may auto-retry after a
# true timeout. Commits, pushes, migrations, deployments, and
# anything explicitly listed in the prompt's forbidden set MUST NOT
# be in this list — even one accidental retry there can corrupt
# production state.
_IDEMPOTENT_READ_TOOLS_DEFAULT: tuple[str, ...] = (
    "search",
    "search.web",
    "search.code",
    "git.status",
    "git.log",
    "git.diff",
)


def should_retry_timeout(
    *,
    tool_name: str | None,
    trip_kind: str,
    classified_reason: TimeoutReason,
    allowed_tools: Iterable[str] | None = None,
) -> RetryDecision:
    """Decide whether a watchdog trip is safe to auto-retry.

    The retry policy is conservative by design: only one automatic
    retry, only on demonstrably idempotent operations, only on an
    idle timeout (never on the absolute cap), and only when the
    watchdog's classification agrees the operation likely didn't
    make progress before being killed.

    :param tool_name: Name of the tool whose timeout is being
        evaluated. ``None`` means no tool context.
    :param trip_kind: ``"idle"`` or ``"absolute"``.
    :param classified_reason: The watchdog's classification.
    :param allowed_tools: Override for the idempotent-read set;
        defaults to :data:`_IDEMPOTENT_READ_TOOLS_DEFAULT`.
    :returns: A :class:`RetryDecision` with the policy outcome and
        a human-readable reason.
    """
    allowed = tuple(allowed_tools) if allowed_tools is not None else _IDEMPOTENT_READ_TOOLS_DEFAULT
    if trip_kind == "absolute":
        return RetryDecision(False, "absolute timeout never auto-retries")
    if classified_reason in {TimeoutReason.DEAD_PROCESS, TimeoutReason.FORWARDER_DISCONNECT}:
        return RetryDecision(False, f"{classified_reason.value} is unsafe to auto-retry")
    if not tool_name:
        return RetryDecision(False, "no tool context — refuse to auto-retry")
    if tool_name not in allowed:
        return RetryDecision(
            False,
            f"tool {tool_name!r} is not in the idempotent-read allowlist",
        )
    return RetryDecision(True, f"tool {tool_name!r} is idempotent-read; one retry allowed")


def tracked_pids() -> Iterator[int]:
    """Yield the PIDs the tracker is currently watching.

    Convenience wrapper around :func:`tracked_subprocesses` for
    scaffold-internal iteration.
    """
    for entry in tracked_subprocesses():
        if entry.pid is not None:
            yield entry.pid


# Time-budget knobs surfaced for the scaffold to consume. Centralised
# here so issue #30's scope (separate model-stream / tool-output /
# max-tool / max-turn windows) is in one place. The scaffold reads
# these via :func:`resolve_watchdog_budgets`.

_DEFAULT_MODEL_STREAM_IDLE_S = 240.0
_DEFAULT_TOOL_OUTPUT_IDLE_S = 240.0
_DEFAULT_MAX_TOOL_RUNTIME_S = 3600.0
_DEFAULT_MAX_TURN_RUNTIME_S = 7200.0


@dataclass(frozen=True)
class WatchdogBudgets:
    """Resolved per-watchdog time budgets for one turn.

    :param model_stream_idle_s: Max seconds the harness may go
        without an emit while no tool is in flight before the
        idle watchdog fires.
    :param tool_output_idle_s: Max seconds a supervised tool may
        go without I/O before the idle watchdog fires.
    :param max_tool_runtime_s: Per-tool absolute cap. ``<= 0``
        disables.
    :param max_turn_runtime_s: Total-turn cap. ``<= 0`` disables.
    """

    model_stream_idle_s: float
    tool_output_idle_s: float
    max_tool_runtime_s: float
    max_turn_runtime_s: float


def resolve_watchdog_budgets() -> WatchdogBudgets:
    """Resolve the per-turn watchdog budgets from env vars.

    Honours the legacy ``HARNESS_TURN_TIMEOUT_S`` as a fallback for
    ``model_stream_idle_s`` and ``HARNESS_TURN_ABSOLUTE_TIMEOUT_S``
    as a fallback for ``max_turn_runtime_s`` so a deployment that
    pinned the older knobs keeps working. Specific env vars take
    precedence.

    :returns: A populated :class:`WatchdogBudgets`.
    """
    legacy_idle = os.environ.get("HARNESS_TURN_TIMEOUT_S")
    legacy_idle_value = (
        float(legacy_idle) if legacy_idle is not None else _DEFAULT_MODEL_STREAM_IDLE_S
    )
    legacy_absolute = os.environ.get("HARNESS_TURN_ABSOLUTE_TIMEOUT_S")
    legacy_absolute_value = (
        float(legacy_absolute) if legacy_absolute is not None else _DEFAULT_MAX_TURN_RUNTIME_S
    )

    def _read(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            _logger.warning("invalid %s=%r; falling back to %s", name, raw, default)
            return default

    return WatchdogBudgets(
        model_stream_idle_s=_read("HARNESS_MODEL_STREAM_IDLE_S", legacy_idle_value),
        tool_output_idle_s=_read("HARNESS_TOOL_OUTPUT_IDLE_S", legacy_idle_value),
        max_tool_runtime_s=_read("HARNESS_MAX_TOOL_RUNTIME_S", _DEFAULT_MAX_TOOL_RUNTIME_S),
        max_turn_runtime_s=_read("HARNESS_MAX_TURN_RUNTIME_S", legacy_absolute_value),
    )
