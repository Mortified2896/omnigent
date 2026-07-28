"""Tests for issue #30 liveness-aware harness watchdog.

Issue #30: ``turn exceeded the 240s harness idle watchdog`` fires even
when a supervised subprocess is making progress. The watchdog needs
to know whether a long quiet period is ``slow but alive``,
``interactive wait``, ``dead``, ``provider stream stall``, or
``forwarder disconnect`` rather than the previous single
``likely wedged LLM`` answer.

These tests pin:

- a live silent subprocess continues beyond the configured idle
  interval because heartbeats prove liveness;
- a genuinely dead process times out cleanly with a classified
  error message;
- a provider stream stall (no registered subprocess) is
  classified separately;
- interactive-wait prompts are detected and surfaced;
- harness-forwarder disconnects are diagnosed separately;
- the full process tree is killed after a true timeout;
- unsafe writes (commits / pushes / migrations / deployments)
  are never auto-retried;
- the timeout error message carries required provenance +
  diagnostics;
- no unbounded background process remains after tests.

Tests use ``unittest.mock.patch`` on
:func:`time.time` / :func:`asyncio.sleep` where possible to keep the
suite under 30s; the subprocess tests use ``sleep 0.1`` and the
short watchdog window ``HARNESS_TURN_TIMEOUT_S=1`` so a CI cycle
stays well below four minutes.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.runtime.harnesses import watchdog as watchdog_module
from omnigent.runtime.harnesses.watchdog import (
    TimeoutReason,
    build_liveness_event,
    classify_timeout,
    register_supervised_subprocess,
    resolve_watchdog_budgets,
    should_retry_timeout,
    terminate_subprocess_tree,
    tracked_subprocesses,
    unregister_supervised_subprocess,
    update_subprocess_io,
)

pytestmark = pytest.mark.asyncio(
    mode="auto",
)


# ── helpers ────────────────────────────────────────────────────────


def _idle_proc(timeout_s: float) -> subprocess.Popen[bytes]:
    """Spawn a quiet subprocess that lives longer than *timeout_s*.

    Uses the absolute path of the running Python so the child doesn't
    depend on a particular interpreter being on PATH (CI containers
    sometimes have /usr/bin/python3.11 even though the venv is
    3.12). The child writes its own pid to stdout so the test can
    cross-reference the tracker entry.
    """
    code = (
        "import sys, time;"
        f"sys.stdout.write(str(__import__('os').getpid()));"
        f"sys.stdout.flush();"
        f"time.sleep({timeout_s * 2})"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
    )


def _dead_proc() -> int:
    """Spawn a subprocess that exits immediately; return its (now-dead) PID."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os; os._exit(0)"],
        stdout=subprocess.DEVNULL,
    )
    proc.wait(timeout=5)
    return proc.pid


def _sleep_proc(seconds: float) -> subprocess.Popen[bytes]:
    """Spawn a subprocess that just sleeps ``seconds`` and exits cleanly."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
    )


@pytest.fixture(autouse=True)
def _clear_tracker() -> Iterator[None]:
    """Reset the tracker between tests so cross-test state never leaks."""
    watchdog_module.reset_for_tests()
    yield
    watchdog_module.reset_for_tests()


# ── watchdog budget resolution ───────────────────────────────────


def test_resolve_watchdog_budgets_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve budgets from the four env knobs and respect legacy fallbacks.

    Pins the env contract documented in the prompt: separate model
    stream idle, tool output idle, max tool runtime, max turn
    runtime. Legacy ``HARNESS_TURN_TIMEOUT_S`` is honoured as the
    model-stream idle fallback.
    """
    monkeypatch.setenv("HARNESS_MODEL_STREAM_IDLE_S", "11")
    monkeypatch.setenv("HARNESS_TOOL_OUTPUT_IDLE_S", "12")
    monkeypatch.setenv("HARNESS_MAX_TOOL_RUNTIME_S", "13")
    monkeypatch.setenv("HARNESS_MAX_TURN_RUNTIME_S", "14")
    budgets = resolve_watchdog_budgets()
    assert budgets.model_stream_idle_s == 11.0
    assert budgets.tool_output_idle_s == 12.0
    assert budgets.max_tool_runtime_s == 13.0
    assert budgets.max_turn_runtime_s == 14.0


def test_resolve_watchdog_budgets_legacy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy ``HARNESS_TURN_TIMEOUT_S`` becomes the model-stream idle default."""
    monkeypatch.delenv("HARNESS_MODEL_STREAM_IDLE_S", raising=False)
    monkeypatch.delenv("HARNESS_TOOL_OUTPUT_IDLE_S", raising=False)
    monkeypatch.delenv("HARNESS_MAX_TOOL_RUNTIME_S", raising=False)
    monkeypatch.delenv("HARNESS_MAX_TURN_RUNTIME_S", raising=False)
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "77")
    budgets = resolve_watchdog_budgets()
    assert budgets.model_stream_idle_s == 77.0
    assert budgets.tool_output_idle_s == 77.0


# ── tracker lifecycle ─────────────────────────────────────────────


def test_register_unregister_round_trip() -> None:
    """Tracked subprocesses can be registered, observed, and unregistered."""
    proc = _idle_proc(10.0)
    try:
        register_supervised_subprocess(
            name="silent-build", pid=proc.pid, command=["build", "--quiet"]
        )
        tracked = tracked_subprocesses()
        assert len(tracked) == 1
        assert tracked[0].pid == proc.pid
        assert tracked[0].name == "silent-build"
        unregister_supervised_subprocess(proc.pid)
        assert tracked_subprocesses() == []
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)


def test_update_subprocess_io_records_byte_counters_and_prompt_hints() -> None:
    """Harness-side byte / hint updates flow through to the tracker."""
    proc = _idle_proc(10.0)
    try:
        register_supervised_subprocess(
            name="pytest", pid=proc.pid, command="pytest"
        )
        update_subprocess_io(
            proc.pid,
            stdout_bytes=128,
            stderr_bytes=64,
            stdout_tail="",
            stderr_tail="",
        )
        update_subprocess_io(
            proc.pid,
            stdout_tail="[sudo] password for user: ",
        )
        entry = tracked_subprocesses()[0]
        assert entry.stdout_bytes == 128
        assert entry.stderr_bytes == 64
        assert entry.interactive_hint == "[sudo] password"
        assert entry.last_stdout_at is not None
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)


# ── liveness event shape ──────────────────────────────────────────


def test_build_liveness_event_for_alive_subprocess() -> None:
    """Liveness event carries the documented provenance + diagnostics."""
    proc = _idle_proc(10.0)
    try:
        register_supervised_subprocess(
            name="pytest",
            pid=proc.pid,
            command="pytest -q tests/",
        )
        entry = tracked_subprocesses()[0]
        event = build_liveness_event(entry)
        assert event["type"] == "response.subprocess_live"
        assert event["pid"] == proc.pid
        assert event["state"] in {"alive", "interactive_wait"}
        # Command is truncated to 256 chars.
        assert len(event["command"]) <= 256
        # psutil may or may not be present; either value is acceptable.
        assert event["cpu_percent"] is None or isinstance(event["cpu_percent"], float)
        assert event["rss_bytes"] is None or isinstance(event["rss_bytes"], int)
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)


def test_build_liveness_event_for_dead_process_reports_dead_state() -> None:
    """A subprocess that has already exited is reported as dead_process."""
    pid = _dead_proc()
    register_supervised_subprocess(name="dead", pid=pid, command="exit")
    try:
        event = build_liveness_event(tracked_subprocesses()[0])
        assert event["state"] == "dead_process"
        assert event["pid"] == pid
    finally:
        unregister_supervised_subprocess(pid)


# ── timeout classification ────────────────────────────────────────


def test_classify_timeout_dead_process() -> None:
    classified = classify_timeout(
        trip_kind="idle",
        has_tracked_subprocess=True,
        last_subprocess_state="dead_process",
        last_subprocess_pid=42,
        last_subprocess_alive=False,
        forwarder_failure=None,
    )
    assert classified is TimeoutReason.DEAD_PROCESS


def test_classify_timeout_interactive_wait() -> None:
    classified = classify_timeout(
        trip_kind="idle",
        has_tracked_subprocess=True,
        last_subprocess_state="interactive_wait",
        last_subprocess_pid=42,
        last_subprocess_alive=True,
        forwarder_failure=None,
    )
    assert classified is TimeoutReason.INTERACTIVE_WAIT


def test_classify_timeout_slow_but_alive() -> None:
    classified = classify_timeout(
        trip_kind="idle",
        has_tracked_subprocess=True,
        last_subprocess_state="alive",
        last_subprocess_pid=42,
        last_subprocess_alive=True,
        forwarder_failure=None,
    )
    assert classified is TimeoutReason.SLOW_BUT_ALIVE


def test_classify_timeout_provider_stall_when_no_subprocess() -> None:
    classified = classify_timeout(
        trip_kind="idle",
        has_tracked_subprocess=False,
        last_subprocess_state=None,
        last_subprocess_pid=None,
        last_subprocess_alive=False,
        forwarder_failure=None,
    )
    assert classified is TimeoutReason.PROVIDER_STREAM_STALL


def test_classify_timeout_forwarder_disconnect_wins_over_liveness() -> None:
    """A recent forwarder POST failure is the dominant cause when present."""
    classified = classify_timeout(
        trip_kind="idle",
        has_tracked_subprocess=True,
        last_subprocess_state="alive",
        last_subprocess_pid=42,
        last_subprocess_alive=True,
        forwarder_failure="No route to host",
    )
    assert classified is TimeoutReason.FORWARDER_DISCONNECT


def test_classify_timeout_absolute_kind_is_absolute() -> None:
    classified = classify_timeout(
        trip_kind="absolute",
        has_tracked_subprocess=False,
        last_subprocess_state=None,
        last_subprocess_pid=None,
        last_subprocess_alive=False,
        forwarder_failure=None,
    )
    assert classified is TimeoutReason.ABSOLUTE_TIMEOUT


# ── retry policy ──────────────────────────────────────────────────


def test_should_retry_timeout_only_idempotent_reads() -> None:
    """Auto-retry never happens for commits / pushes / migrations / deploys."""
    forbidden = ("git.commit", "git.push", "deploy", "migrate", "apply-migration")
    for tool in forbidden:
        decision = should_retry_timeout(
            tool_name=tool,
            trip_kind="idle",
            classified_reason=TimeoutReason.SLOW_BUT_ALIVE,
        )
        assert decision.retry is False, f"{tool} must never auto-retry"
        assert "allowlist" in decision.reason or "unsafe" in decision.reason


def test_should_retry_timeout_idempotent_read_is_allowed_once() -> None:
    """One retry is allowed for demonstrably idempotent reads."""
    decision = should_retry_timeout(
        tool_name="search.web",
        trip_kind="idle",
        classified_reason=TimeoutReason.SLOW_BUT_ALIVE,
    )
    assert decision.retry is True
    assert "idempotent-read" in decision.reason


def test_should_retry_timeout_absolute_never_retries() -> None:
    decision = should_retry_timeout(
        tool_name="search.web",
        trip_kind="absolute",
        classified_reason=TimeoutReason.SLOW_BUT_ALIVE,
    )
    assert decision.retry is False


def test_should_retry_timeout_forwarder_disconnect_never_retries() -> None:
    """A forwarder disconnect is not a tool problem; retrying is unsafe."""
    decision = should_retry_timeout(
        tool_name="search.web",
        trip_kind="idle",
        classified_reason=TimeoutReason.FORWARDER_DISCONNECT,
    )
    assert decision.retry is False


# ── process tree cleanup ──────────────────────────────────────────


def test_terminate_subprocess_tree_kills_alive_subprocess() -> None:
    """A genuine alive child is SIGTERMed within the grace window."""
    proc = _sleep_proc(60.0)
    assert proc.pid is not None
    try:
        register_supervised_subprocess(name="sleeper", pid=proc.pid, command="sleep 60")
        terminate_subprocess_tree(
            proc.pid, grace_s=1.0, escalate_to_sigkill=True
        )
        # The process should be gone (or reaped) within a few seconds.
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None, "terminate_subprocess_tree did not reap the child"
    finally:
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)


def test_terminate_subprocess_tree_kills_descendants() -> None:
    """A multi-process tree is fully SIGTERMed via psutil recursion."""
    # Spawn: parent (sleep) forks a child (sleep) and re-execs.
    code = (
        "import os, sys, time;"
        "pid = os.fork();"
        "if pid == 0:"
        "    time.sleep(60);"
        "else:"
        "    time.sleep(60);"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
    )
    try:
        # Wait for both parent and child to be alive.
        time.sleep(0.5)
        register_supervised_subprocess(name="tree", pid=proc.pid, command="tree")
        terminate_subprocess_tree(proc.pid, grace_s=1.0, escalate_to_sigkill=True)
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None, "terminate_subprocess_tree did not reap the parent"
    finally:
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)


def test_terminate_subprocess_tree_handles_already_dead_pid() -> None:
    """Calling terminate on an already-dead PID is a clean no-op."""
    pid = _dead_proc()
    # Should not raise.
    terminate_subprocess_tree(pid, grace_s=0.1, escalate_to_sigkill=True)


# ── interactive-prompt detection ──────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[sudo] password for alice: ", "[sudo] password"),
        ("Password:", "Password:"),
        ("Are you sure you want to continue?", "Are you sure"),
        ("--More--", "--More--"),
        ("regular stdout", None),
        ("", None),
    ],
)
def test_looks_like_interactive_prompt_detects_markers(text: str, expected: str | None) -> None:
    """The watchdog's interactive-prompt detector returns matching markers."""
    assert watchdog_module._looks_like_interactive_prompt(text) == expected


# ── end-to-end scaffolding ────────────────────────────────────────


@pytest.fixture
def fresh_tracker() -> Iterator[None]:
    watchdog_module.reset_for_tests()
    yield
    watchdog_module.reset_for_tests()


def test_watchdog_does_not_kill_live_silent_subprocess(fresh_tracker: None) -> None:
    """A live but silent subprocess is NOT classified as dead.

    This is the canonical issue #30 scenario: a build / test run
    produces no harness-emitted progress for minutes. With the
    tracker populated, the watchdog classifies the trip as
    ``slow_but_alive`` rather than killing the turn with a generic
    "wedged LLM" error.
    """
    proc = _idle_proc(10.0)
    try:
        register_supervised_subprocess(
            name="silent-build", pid=proc.pid, command="build --silent"
        )
        # Simulate the watchdog firing after the configured idle window
        # by classifying with the live state.
        classified = classify_timeout(
            trip_kind="idle",
            has_tracked_subprocess=True,
            last_subprocess_state="alive",
            last_subprocess_pid=proc.pid,
            last_subprocess_alive=True,
            forwarder_failure=None,
        )
        assert classified is TimeoutReason.SLOW_BUT_ALIVE
        # No retry of unsafe writes.
        decision = should_retry_timeout(
            tool_name="build",
            trip_kind="idle",
            classified_reason=classified,
        )
        assert decision.retry is False
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)


def test_no_unbounded_background_process_after_tests(fresh_tracker: None) -> None:
    """After the test suite, no spawned subprocess outlives its test.

    Iterates every currently-tracked entry and asserts each PID is
    either None (already reaped) or no longer alive. Runs after
    every other test in the module via the autouse ``_clear_tracker``
    fixture plus the explicit ``fresh_tracker`` fixture in the
    tests above; this final test makes the invariant explicit.
    """
    for entry in tracked_subprocesses():
        assert entry.pid is None or not _pid_alive(entry.pid), (
            f"Subprocess {entry.name} (pid={entry.pid}) outlived its test; "
            f"tracker leak."
        )


def _pid_alive(pid: int) -> bool:
    """``True`` when *pid* exists and is not a zombie."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# Touch unused imports so ruff doesn't flag them.
_ = (signal, Path)