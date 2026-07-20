"""Bounded lifecycle manager for runner-owned ``opencode serve`` instances.

The runner owns one ``opencode serve`` subprocess per opencode-native
conversation. Idle sessions previously kept both the HTTP server
(``opencode serve`` ≈ 510 MB RSS) and the embedded TUI (the
``opencode attach`` pane ≈ 300 MB RSS) warm for the conversation
lifetime — six idle sessions cost ~4.8 GB on a shared runner, and the
existing SDK-pane reaper (30-minute default) and ``HarnessProcessManager``
(30-minute SDK subprocess reaper) operate at the wrong layer to reclaim
the opencode HTTP server. This module adds a small, reusable pool that:

  * hibernates an idle ``opencode serve`` after a configurable window
    (default 300 s), preserving the durable OpenCode session id so the
    next prompt resumes transparently;
  * enforces a configurable maximum number of warm servers (default 2)
    so a third prompt evicts the least-recently-used idle runtime rather
    than starting a third server;
  * serializes cold start, resume, prompt dispatch, hibernation, and
    archive/delete teardown through a per-conversation lifecycle lock,
    so a prompt racing with the reaper cannot produce duplicate
    servers or a stale bridge URL.

It is intentionally narrow — it is **not** a general harness framework.
The policy layer (idle clocks, LRU, capacity, second busy check) is
separated from the opencode adapter (``OpenCodeAdapter``) so a future
Codex-native or custom native-server adapter can plug in by implementing
``start`` / ``resume`` / ``is_busy`` / ``hibernate`` / ``terminate`` /
``resource_weight``.

State machine:

    starting → awake → (busy ↔ idle) → hibernating → hibernated
                          │
                          └→ failed   (transient; recovered by the
                                       next ``ensure_awake`` call)

Hibernation is an explicit, recoverable state — the public conversation
session never becomes ``failed`` because its opencode runtime was
intentionally hibernated. Bridge state retains ``external_session_id``
and ``model_override`` / ``reasoning_effort`` / ``permission_mode`` /
``route_approved`` across hibernations so resume preserves the user's
selection; only the live ``server_base_url`` and ``auth_secret`` are
cleared (they would otherwise look like a usable URL pointing at a dead
server).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_logger = logging.getLogger(__name__)


# ── Configuration (env-driven, fail-safe) ────────────────────────────────────

# Server idle timeout. After this many seconds of inactivity (no prompt /
# response / tool / status / approval / question / terminal activity) the
# runner hibernates the opencode runtime.
_SERVER_IDLE_TIMEOUT_ENV = "OMNIGENT_OPENCODE_SERVER_IDLE_TIMEOUT_S"
_SERVER_IDLE_TIMEOUT_DEFAULT_S = 300.0

# Maximum number of warm opencode servers kept on the runner. A wake
# request beyond this limit first hibernates the LRU idle runtime.
_MAX_WARM_SERVERS_ENV = "OMNIGENT_OPENCODE_MAX_WARM_SERVERS"
_MAX_WARM_SERVERS_DEFAULT = 2

# Reaper scan interval.
_REAPER_INTERVAL_ENV = "OMNIGENT_OPENCODE_REAPER_INTERVAL_S"
_REAPER_INTERVAL_DEFAULT_S = 30.0

# Per-pane attach timeout (the opencode attach TUI). This is also
# honored by the ``NativePaneReaper`` for the ``opencode`` terminal
# name; the value lives here as the runtime-manager-side source of
# truth so a single env knob governs both layers.
_ATTACH_IDLE_TIMEOUT_ENV = "OMNIGENT_OPENCODE_ATTACH_IDLE_TIMEOUT_S"
_ATTACH_IDLE_TIMEOUT_DEFAULT_S = 120.0

# Maximum wait for capacity before returning the retryable capacity
# error. ``0`` disables the wait (fail immediately).
_CAPACITY_WAIT_TIMEOUT_ENV = "OMNIGENT_OPENCODE_CAPACITY_WAIT_TIMEOUT_S"
_CAPACITY_WAIT_TIMEOUT_DEFAULT_S = 60.0


class OpenCodeRuntimeError(RuntimeError):
    """Raised when the opencode runtime pool rejects a wake with capacity full."""


class CapacityBusyError(OpenCodeRuntimeError):
    """Raised when the runner cannot wake a runtime within the configured wait.

    ``code="opencode_capacity_busy"`` is the API surface code; the message is
    operator/client-safe (no internal paths or PIDs).
    """

    code: str = "opencode_capacity_busy"


def resolve_server_idle_timeout_s() -> float:
    """Resolve the opencode server idle window in seconds.

    Honors :envvar:`OMNIGENT_OPENCODE_SERVER_IDLE_TIMEOUT_S`. ``0`` disables
    server hibernation; negative / unparseable values fall back to the default
    so a typo at boot never silently turns the reaper into a "reap
    everything on first pass" landmine.
    """
    return _resolve_positive_timeout_env(
        _SERVER_IDLE_TIMEOUT_ENV, default=_SERVER_IDLE_TIMEOUT_DEFAULT_S
    )


def resolve_max_warm_servers() -> int:
    """Resolve the maximum number of warm opencode servers kept by the runner.

    Honors :envvar:`OMNIGENT_OPENCODE_MAX_WARM_SERVERS`. Must be at least 1
    unless the operator explicitly sets ``0`` (which is reserved for
    "unbounded" only when this convention is later introduced; today ``0``
    falls back to the default — silently unbounded capacity would defeat
    the memory-safety guarantee). Unparseable / negative values fall back to
    the default.
    """
    raw = os.environ.get(_MAX_WARM_SERVERS_ENV)
    if not raw:
        return int(_MAX_WARM_SERVERS_DEFAULT)
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "%s=%r is not an integer; using default %d",
            _MAX_WARM_SERVERS_ENV,
            raw,
            _MAX_WARM_SERVERS_DEFAULT,
        )
        return int(_MAX_WARM_SERVERS_DEFAULT)
    if value < 1:
        _logger.warning(
            "%s=%r must be at least 1; using default %d",
            _MAX_WARM_SERVERS_ENV,
            raw,
            _MAX_WARM_SERVERS_DEFAULT,
        )
        return int(_MAX_WARM_SERVERS_DEFAULT)
    return value


def resolve_reaper_interval_s() -> float:
    """Resolve the opencode reaper scan interval in seconds."""
    return _resolve_positive_timeout_env(_REAPER_INTERVAL_ENV, default=_REAPER_INTERVAL_DEFAULT_S)


def resolve_attach_idle_timeout_s() -> float:
    """Resolve the per-pane opencode attach idle window in seconds.

    ``0`` disables attach reaping for opencode. Used by the
    :class:`omnigent.terminals.pane_reaper.NativePaneReaper` per-harness
    resolver as the single source of truth.
    """
    return _resolve_positive_timeout_env(
        _ATTACH_IDLE_TIMEOUT_ENV, default=_ATTACH_IDLE_TIMEOUT_DEFAULT_S
    )


def resolve_capacity_wait_timeout_s() -> float:
    """Resolve the bounded capacity-wait timeout in seconds."""
    return _resolve_positive_timeout_env(
        _CAPACITY_WAIT_TIMEOUT_ENV, default=_CAPACITY_WAIT_TIMEOUT_DEFAULT_S
    )


def _resolve_positive_timeout_env(env_name: str, *, default: float) -> float:
    """Resolve a positive numeric env var, falling back to ``default`` on bad input."""
    raw = os.environ.get(env_name)
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        _logger.warning(
            "%s=%r is not a number; using default %ss",
            env_name,
            raw,
            default,
        )
        return float(default)
    if value < 0:
        _logger.warning(
            "%s=%r is negative; using default %ss",
            env_name,
            raw,
            default,
        )
        return float(default)
    return value


# ── Lifecycle state ──────────────────────────────────────────────────────────


class LifecycleState(str, Enum):
    """Per-runtime lifecycle state (OpenCode adapter output)."""

    STARTING = "starting"
    AWAKE = "awake"
    BUSY = "busy"
    HIBERNATING = "hibernating"
    HIBERNATED = "hibernated"
    STOPPING = "stopping"
    FAILED = "failed"


# ── Runtime entry ────────────────────────────────────────────────────────────


@dataclass
class OpenCodeRuntimeEntry:
    """Per-conversation runtime metadata held by the pool.

    The pool is the single owner of the entries dict; per-conversation lock
    + pool-level capacity lock serialize all mutations.
    """

    conversation_id: str
    state: LifecycleState = LifecycleState.STARTING
    last_activity_at: float = field(default_factory=time.monotonic)
    started_at: float = field(default_factory=time.monotonic)
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    external_session_id: str | None = None
    # Free-form adapter metadata (e.g. server PID, attach PID). Sanitized for
    # the diagnostic surface — secrets and full env never appear here.
    adapter_metadata: dict[str, Any] = field(default_factory=dict)

    def is_busy(self) -> bool:
        """Conservative busy signal — set when the runtime is in a busy state."""
        busy_states = (
            LifecycleState.STARTING,
            LifecycleState.BUSY,
            LifecycleState.HIBERNATING,
        )
        return self.state in busy_states


# ── Adapter interface (opencode-specific) ────────────────────────────────────


class OpenCodeAdapter:
    """Adapter the pool delegates opencode-specific work to.

    The pool knows nothing about tmux / ``opencode serve`` / bridge state —
    every concrete side effect goes through this interface so a future
    Codex-native / custom adapter can drop in by providing its own.

    The adapter MUST be safe to call with no live processes (return clean
    defaults) so the pool can use it during orphan reconciliation without
    first materializing a runtime.
    """

    async def is_busy(self, conversation_id: str) -> bool:
        """Return ``True`` if the conversation's opencode runtime is busy."""
        raise NotImplementedError

    async def has_attached_tmux_client(self, conversation_id: str) -> bool:
        """Return ``True`` if a human is attached to the opencode pane."""
        raise NotImplementedError

    async def hibernate(self, conversation_id: str, *, reason: str) -> bool:
        """Hibernate (stop attach + forwarder + server) one runtime.

        :returns: ``True`` when teardown ran; ``False`` when the runtime is
            already gone (idempotent).
        """
        raise NotImplementedError

    async def terminate(self, conversation_id: str, *, reason: str) -> bool:
        """Permanently remove one runtime (delete / archive / shutdown).

        :returns: ``True`` when teardown ran; ``False`` when the runtime is
            already gone.
        """
        raise NotImplementedError

    async def shutdown(self) -> None:
        """Tear down every owned runtime. Idempotent."""
        raise NotImplementedError

    def warm_count(self) -> int:
        """Return the number of warm opencode servers currently owned."""
        raise NotImplementedError

    def attach_pid(self, conversation_id: str) -> int | None:
        """Best-effort attach-process PID for diagnostics; ``None`` if unknown."""
        raise NotImplementedError


# ── Pool ─────────────────────────────────────────────────────────────────────


@dataclass
class _PoolConfig:
    """Validated pool configuration."""

    idle_timeout_s: float
    max_warm_servers: int
    reaper_interval_s: float
    attach_idle_timeout_s: float
    capacity_wait_timeout_s: float


def _resolve_pool_config() -> _PoolConfig:
    return _PoolConfig(
        idle_timeout_s=resolve_server_idle_timeout_s(),
        max_warm_servers=resolve_max_warm_servers(),
        reaper_interval_s=resolve_reaper_interval_s(),
        attach_idle_timeout_s=resolve_attach_idle_timeout_s(),
        capacity_wait_timeout_s=resolve_capacity_wait_timeout_s(),
    )


class OpenCodeRuntimePool:
    """Bounded pool of runner-owned opencode runtimes with idle hibernation.

    :param adapter: OpenCode-specific side-effect delegate.
    :param idle_timeout_s: Per-runtime idle window before hibernation. ``0``
        disables hibernation.
    :param max_warm_servers: Maximum number of warm servers. ``>= 1``.
    :param reaper_interval_s: Seconds between idle scans.
    :param capacity_wait_timeout_s: Maximum bounded wait for capacity.
        ``0`` rejects immediately.
    """

    def __init__(
        self,
        *,
        adapter: OpenCodeAdapter,
        idle_timeout_s: float | None = None,
        max_warm_servers: int | None = None,
        reaper_interval_s: float | None = None,
        capacity_wait_timeout_s: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        config = _resolve_pool_config()
        self._adapter = adapter
        self._idle_timeout_s = float(
            idle_timeout_s if idle_timeout_s is not None else config.idle_timeout_s
        )
        self._max_warm_servers = int(
            max_warm_servers if max_warm_servers is not None else config.max_warm_servers
        )
        self._reaper_interval_s = float(
            reaper_interval_s if reaper_interval_s is not None else config.reaper_interval_s
        )
        self._capacity_wait_timeout_s = float(
            capacity_wait_timeout_s
            if capacity_wait_timeout_s is not None
            else config.capacity_wait_timeout_s
        )
        self._clock = clock or time.monotonic
        self._entries: dict[str, OpenCodeRuntimeEntry] = {}
        # Top-level lock for the entries dict + capacity decisions; held only
        # long enough to mutate state, never across I/O.
        self._capacity_lock = asyncio.Lock()
        # Per-conversation lifecycle locks live inside the entries themselves
        # so the lock outlives a hot restart and a re-creation acquires the
        # same one. (asyncio.Lock instances are not strictly reusable across
        # loops, but a fresh entry on a fresh pool is fine — see
        # ``OpenCodeRuntimeEntry.lifecycle_lock``.)
        self._task: asyncio.Task[None] | None = None
        self._started = False

    # ── Configuration accessors (used by the runner to push the same value
    # into the pane reaper so a single env knob governs both layers) ─────

    @property
    def idle_timeout_s(self) -> float:
        """Idle window before a server is hibernated."""
        return self._idle_timeout_s

    @property
    def attach_idle_timeout_s(self) -> float:
        """Per-pane opencode attach idle window (passed to ``NativePaneReaper``)."""
        return float(resolve_attach_idle_timeout_s())

    @property
    def max_warm_servers(self) -> int:
        """Maximum number of warm opencode servers kept by the pool."""
        return self._max_warm_servers

    @property
    def capacity_wait_timeout_s(self) -> float:
        """Maximum bounded wait for capacity before raising ``CapacityBusyError``."""
        return self._capacity_wait_timeout_s

    @property
    def reaper_interval_s(self) -> float:
        """Seconds between idle scans."""
        return self._reaper_interval_s

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the reaper loop (idempotent)."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._reap_loop(), name="opencode-runtime-pool-reaper")
        _logger.info(
            "opencode runtime pool started (idle_timeout=%ss, max_warm=%d, "
            "reaper_interval=%ss, capacity_wait=%ss%s)",
            self._idle_timeout_s,
            self._max_warm_servers,
            self._reaper_interval_s,
            self._capacity_wait_timeout_s,
            "; hibernation DISABLED" if self._idle_timeout_s <= 0 else "",
        )

    async def shutdown(self) -> None:
        """Cancel the reaper loop and tear down every owned runtime."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._started = False
        await self._adapter.shutdown()
        # Drop bookkeeping entries so post-shutdown snapshots are empty.
        # The adapter has already killed every owned server; nothing to
        # wait on per-entry.
        self._entries.clear()

    # ── Public API ───────────────────────────────────────────────────────

    def register(self, conversation_id: str, entry: OpenCodeRuntimeEntry) -> None:
        """Idempotently register / replace a runtime entry for *conversation_id*.

        The caller is responsible for actually starting the server; this
        method only inserts the bookkeeping entry.
        """
        existing = self._entries.get(conversation_id)
        if existing is not None:
            # Preserve the lifecycle lock so a concurrent hibernate can't race
            # with the new register (e.g. relaunch during a hibernation scan).
            entry.lifecycle_lock = existing.lifecycle_lock
            entry.state = LifecycleState.AWAKE
            entry.last_activity_at = self._clock()
        else:
            entry.lifecycle_lock = asyncio.Lock()
            entry.state = LifecycleState.AWAKE
            entry.last_activity_at = self._clock()
        self._entries[conversation_id] = entry

    async def mark_activity(self, conversation_id: str) -> None:
        """Refresh the idle clock for *conversation_id*.

        Called from forwarder events (text delta / tool call / status),
        from the prompt dispatch path, and from approval / question
        activity. No-op when the conversation has no live runtime — the
        next ``ensure_awake`` call will create one.
        """
        entry = self._entries.get(conversation_id)
        if entry is None:
            return
        entry.last_activity_at = self._clock()

    def has_entry(self, conversation_id: str) -> bool:
        """Whether the pool tracks *conversation_id* (any state)."""
        return conversation_id in self._entries

    def get(self, conversation_id: str) -> OpenCodeRuntimeEntry | None:
        """Return the entry for *conversation_id* (read-only snapshot)."""
        return self._entries.get(conversation_id)

    async def is_busy(self, conversation_id: str) -> bool:
        """Whether the runtime is busy (defers to the adapter)."""
        entry = self._entries.get(conversation_id)
        if entry is None:
            return False
        if entry.is_busy():
            return True
        # Delegate to the adapter (cross-checks bridge state, forwarder,
        # attached client, etc. — see ``OpenCodeAdapter``).
        return bool(await self._adapter.is_busy(conversation_id))

    async def has_attached_tmux_client(self, conversation_id: str) -> bool:
        """Whether a human is attached to the conversation's opencode pane."""
        return bool(await self._adapter.has_attached_tmux_client(conversation_id))

    async def ensure_awake(self, conversation_id: str) -> OpenCodeRuntimeEntry | None:
        """Ensure a runtime entry exists and is not hibernated.

        Called before any operation that needs a live runtime (cold start,
        resume, prompt dispatch). If the pool is at capacity the caller is
        blocked behind the configured wait, after which a ``CapacityBusyError``
        is raised so the API can return the documented retryable error.

        :param conversation_id: Session id, e.g. ``"conv_abc123"``.
        :returns: The entry (existing or freshly registered), or ``None``
            when the requester is the reaper and no entry exists.
        :raises CapacityBusyError: When capacity cannot be freed within
            the configured wait.
        """
        entry = self._entries.get(conversation_id)
        if entry is not None and entry.state != LifecycleState.HIBERNATED:
            return entry
        # Need capacity. Race for it under the pool lock so concurrent
        # ``ensure_awake`` calls don't both pass the count check.
        deadline = self._clock() + max(self._capacity_wait_timeout_s, 0.0)
        while True:
            acquired = await self._maybe_acquire_capacity(conversation_id)
            if acquired:
                return self._entries.get(conversation_id)
            now = self._clock()
            if now >= deadline or self._capacity_wait_timeout_s <= 0:
                raise CapacityBusyError(
                    "OpenCode runtime capacity is full; retry shortly. "
                    f"(max_warm={self._max_warm_servers}, warm_now="
                    f"{self._adapter.warm_count()})"
                )
            # Re-poll quickly so a freed slot is picked up promptly.
            await asyncio.sleep(min(0.5, max(deadline - now, 0.0)))
            # Re-check after waiting in case the entry was registered in the
            # interim (e.g. the reaper restored it during capacity negotiation).
            entry = self._entries.get(conversation_id)
            if entry is not None and entry.state != LifecycleState.HIBERNATED:
                return entry

    async def _maybe_acquire_capacity(self, requesting_conversation_id: str) -> bool:
        """Try to acquire one warm-server slot. Returns ``True`` on success."""
        async with self._capacity_lock:
            warm = self._adapter.warm_count()
            entry = self._entries.get(requesting_conversation_id)
            # Count the requesting entry as warm when it's awake/starting/busy.
            requester_warm = entry is not None and entry.state in (
                LifecycleState.AWAKE,
                LifecycleState.BUSY,
            )
            # Hibernate the LRU idle candidate if we're at capacity and the
            # requester doesn't already occupy a warm slot.
            if warm >= self._max_warm_servers and not requester_warm:
                candidate = self._select_lru_idle_candidate(requesting_conversation_id)
                if candidate is None:
                    # Genuinely all busy / booting / blocked: no slot can be
                    # freed. The outer wait loop will retry / time out.
                    return False
                # Mark hibernating + release capacity by tearing the candidate
                # down outside the lock. The candidate's own lifecycle_lock
                # serializes against a concurrent prompt dispatch.
                await self._hibernate_entry(candidate.conversation_id, reason="capacity")
            elif warm < self._max_warm_servers:
                # Slot available.
                return True
            elif requester_warm:
                # Requester already owns a warm slot.
                return True
            return False

    def _select_lru_idle_candidate(
        self, requesting_conversation_id: str
    ) -> OpenCodeRuntimeEntry | None:
        """Pick the oldest genuinely-idle runtime, excluding the requester.

        A runtime is idle iff the adapter reports it is NOT busy (no active
        turn, no approval / question pending, no autonomous PTY work, no
        attached tmux client) AND the pool state is ``AWAKE`` / ``STARTING``
        is excluded (we don't kill a starting runtime — a concurrent cold
        start could otherwise evict a requester that just won the race).
        """
        candidates: list[OpenCodeRuntimeEntry] = []
        for conv_id, entry in self._entries.items():
            if conv_id == requesting_conversation_id:
                continue
            if entry.state != LifecycleState.AWAKE:
                continue
            candidates.append(entry)
        if not candidates:
            return None
        candidates.sort(key=lambda e: e.last_activity_at)
        return candidates[0]

    async def hibernate(self, conversation_id: str, *, reason: str) -> bool:
        """Public hibernate entry point. Returns ``True`` on success."""
        async with self._capacity_lock:
            return await self._hibernate_entry(conversation_id, reason=reason)

    async def _hibernate_entry(self, conversation_id: str, *, reason: str) -> bool:
        """Hibernate one entry; returns ``True`` on success (idempotent).

        Caller MUST hold ``_capacity_lock`` so capacity is decremented
        atomically. The candidate's own lifecycle lock serializes against a
        concurrent prompt dispatch, so we never evict an active runtime.
        """
        entry = self._entries.get(conversation_id)
        if entry is None:
            return False
        if entry.state in (LifecycleState.HIBERNATED, LifecycleState.STOPPING):
            return False
        # Per-conversation lock: the prompt dispatch path also takes it, so
        # we never kill a runtime mid-turn.
        async with entry.lifecycle_lock:
            # Recheck inside the lock: a prompt dispatch may have flipped us
            # to BUSY while we were waiting for the capacity lock. Includes
            # the attached-tmux-client signal so a human watching the pane
            # never has the runtime hibernated under them.
            busy = (
                entry.is_busy()
                or await self._adapter.is_busy(conversation_id)
                or await self._adapter.has_attached_tmux_client(conversation_id)
            )
            if busy:
                _logger.info(
                    "opencode pool: hibernate skipped because busy "
                    "(conversation_id=%s, reason=%s)",
                    conversation_id,
                    reason,
                )
                return False
            entry.state = LifecycleState.HIBERNATING
        teardown_ok = False
        try:
            teardown_ok = await self._adapter.hibernate(conversation_id, reason=reason)
        finally:
            if teardown_ok:
                entry.state = LifecycleState.HIBERNATED
                _logger.info(
                    "opencode pool: hibernated (conversation_id=%s, reason=%s, "
                    "warm=%d, capacity=%d, idle=%.1fs)",
                    conversation_id,
                    reason,
                    self._adapter.warm_count(),
                    self._max_warm_servers,
                    self._clock() - entry.last_activity_at,
                )
            else:
                entry.state = LifecycleState.AWAKE
        return teardown_ok

    async def terminate(self, conversation_id: str, *, reason: str) -> bool:
        """Permanently remove a runtime. Idempotent."""
        async with self._capacity_lock:
            entry = self._entries.pop(conversation_id, None)
            if entry is None:
                return False
            async with entry.lifecycle_lock:
                entry.state = LifecycleState.STOPPING
            try:
                return await self._adapter.terminate(conversation_id, reason=reason)
            finally:
                entry.state = LifecycleState.STOPPING

    # ── Reaper loop ──────────────────────────────────────────────────────

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._reaper_interval_s)
            except asyncio.CancelledError:
                return
            # ``<= 0`` disables hibernation entirely (mirrors the SDK reaper).
            if self._idle_timeout_s <= 0:
                continue
            try:
                await self._scan_once()
            except Exception:  # never let a scan error kill the loop
                _logger.exception("opencode runtime pool: scan failed")

    async def _scan_once(self) -> None:
        """Pick an idle LRU candidate and hibernate it.

        Recheck busy state immediately before teardown to close the
        select→reap race. Bounded to one candidate per scan so the loop
        doesn't compete with new wake requests for capacity.
        """
        now = self._clock()
        async with self._capacity_lock:
            # Find the oldest idle AWAKE entry.
            idle: list[OpenCodeRuntimeEntry] = []
            for entry in self._entries.values():
                if entry.state != LifecycleState.AWAKE:
                    continue
                if (now - entry.last_activity_at) < self._idle_timeout_s:
                    continue
                idle.append(entry)
            idle.sort(key=lambda e: e.last_activity_at)
            if not idle:
                return
            candidate = idle[0]
        # Second busy check off the capacity lock. Considers both the
        # adapter's busy signal AND an attached tmux client (the same
        # predicate the native-pane reaper uses) so a human watching the
        # pane cannot have the runtime hibernated under them.
        busy = await self._adapter.is_busy(
            candidate.conversation_id
        ) or await self._adapter.has_attached_tmux_client(candidate.conversation_id)
        if busy:
            _logger.info(
                "opencode pool: hibernation skipped because busy (conversation_id=%s)",
                candidate.conversation_id,
            )
            return
        _logger.info(
            "opencode pool: idle candidate selected (conversation_id=%s, "
            "idle=%.1fs, capacity=%d, warm=%d)",
            candidate.conversation_id,
            self._clock() - candidate.last_activity_at,
            self._max_warm_servers,
            self._adapter.warm_count(),
        )
        await self.hibernate(candidate.conversation_id, reason="idle")

    # ── Diagnostics ──────────────────────────────────────────────────────

    def snapshot(self, *, only_awake: bool = False) -> list[dict[str, Any]]:
        """Return sanitized diagnostic snapshots for operator tooling.

        Excludes any adapter metadata the harness would consider secret
        (auth tokens, provider credentials, generated opencode.json bodies).
        No prompt text or opencode auth contents are emitted.
        """
        out: list[dict[str, Any]] = []
        now = self._clock()
        for conv_id, entry in self._entries.items():
            if only_awake and entry.state != LifecycleState.AWAKE:
                continue
            meta = dict(entry.adapter_metadata)
            sanitized_meta = {
                key: value
                for key, value in meta.items()
                if key in {"server_pid", "attach_pid", "port"}
            }
            out.append(
                {
                    "conversation_id": conv_id,
                    "state": entry.state.value,
                    "last_activity_age_s": round(now - entry.last_activity_at, 3),
                    "started_at": entry.started_at,
                    "external_session_id": entry.external_session_id,
                    **sanitized_meta,
                }
            )
        out.sort(key=lambda row: row["last_activity_age_s"], reverse=True)
        return out

    def warm_count(self) -> int:
        """Forward to the adapter so external callers don't need both handles."""
        return self._adapter.warm_count()


__all__ = [
    "CapacityBusyError",
    "LifecycleState",
    "OpenCodeAdapter",
    "OpenCodeRuntimeEntry",
    "OpenCodeRuntimeError",
    "OpenCodeRuntimePool",
    "resolve_attach_idle_timeout_s",
    "resolve_capacity_wait_timeout_s",
    "resolve_max_warm_servers",
    "resolve_reaper_interval_s",
    "resolve_server_idle_timeout_s",
]
