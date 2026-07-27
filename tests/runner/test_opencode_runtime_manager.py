"""Unit tests for the runner-owned opencode runtime pool.

The bounded pool (default 2 warm servers) and idle hibernation (default
300 s) are the load-bearing pieces of the OpenCode memory-lifecycle
fix. These tests cover the policy layer end-to-end with a fake adapter,
focusing on:

* Idle clock / activity bookkeeping
* Capacity LRU selection + capacity-busy timeout
* Per-conversation lifecycle lock + second-busy re-check
* Configuration resolution (env-var fail-safe semantics)
* Reaper-loop resilience (exceptions don't kill the loop)

Integration coverage (opencode serve/attach teardown, bridge state,
process-tree cleanup, context retention, race tests) lives in
``test_opencode_native_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner.opencode_runtime_manager import (
    CapacityBusyError,
    LifecycleState,
    OpenCodeRuntimeEntry,
    OpenCodeRuntimePool,
    resolve_attach_idle_timeout_s,
    resolve_capacity_wait_timeout_s,
    resolve_max_warm_servers,
    resolve_reaper_interval_s,
    resolve_server_idle_timeout_s,
)


class _FakeAdapter:
    """Adapter double; the unit tests stub busy / warm / lifecycle callbacks."""

    def __init__(
        self,
        *,
        busy: set[str] | None = None,
        attached: set[str] | None = None,
        warm_count: int = 0,
        hibernate_result: bool = True,
        terminate_result: bool = True,
    ) -> None:
        self._busy = set(busy or ())
        self._attached = set(attached or ())
        self._warm_count = warm_count
        self._hibernate_result = hibernate_result
        self._terminate_result = terminate_result
        self.hibernate_calls: list[tuple[str, str]] = []
        self.terminate_calls: list[tuple[str, str]] = []
        self.shutdown_calls = 0

    async def is_busy(self, conversation_id: str) -> bool:
        return conversation_id in self._busy

    async def has_attached_tmux_client(self, conversation_id: str) -> bool:
        return conversation_id in self._attached

    def warm_count(self) -> int:
        return self._warm_count

    def attach_pid(self, conversation_id: str) -> int | None:
        return None

    async def hibernate(self, conversation_id: str, *, reason: str) -> bool:
        self.hibernate_calls.append((conversation_id, reason))
        self._warm_count = max(0, self._warm_count - 1)
        return self._hibernate_result

    async def terminate(self, conversation_id: str, *, reason: str) -> bool:
        self.terminate_calls.append((conversation_id, reason))
        self._warm_count = max(0, self._warm_count - 1)
        return self._terminate_result

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _pool(
    adapter: _FakeAdapter,
    *,
    idle_timeout_s: float = 300.0,
    max_warm_servers: int = 2,
    reaper_interval_s: float = 0.01,
    capacity_wait_timeout_s: float = 0.0,
    clock: Any = None,
) -> OpenCodeRuntimePool:
    return OpenCodeRuntimePool(
        adapter=adapter,
        idle_timeout_s=idle_timeout_s,
        max_warm_servers=max_warm_servers,
        reaper_interval_s=reaper_interval_s,
        capacity_wait_timeout_s=capacity_wait_timeout_s,
        clock=clock,
    )


def _entry(conv_id: str, *, last_activity_at: float) -> OpenCodeRuntimeEntry:
    return OpenCodeRuntimeEntry(
        conversation_id=conv_id,
        state=LifecycleState.AWAKE,
        last_activity_at=last_activity_at,
        started_at=last_activity_at,
        lifecycle_lock=asyncio.Lock(),
    )


def _register_with_past_activity(
    pool: OpenCodeRuntimePool,
    conv_id: str,
    *,
    last_activity_at: float,
) -> OpenCodeRuntimeEntry:
    """Register an entry whose ``last_activity_at`` predates the
    current ``pool._clock()`` value so the reaper picks it up
    immediately.

    ``register`` itself always resets ``last_activity_at`` to the pool
    clock so a freshly-launched runtime gets a full grace window; we
    bypass that with a post-register rewrite for tests that want a
    pre-existing idle entry.
    """
    entry = _entry(conv_id, last_activity_at=pool._clock())
    pool.register(conv_id, entry)
    pool._entries[conv_id].last_activity_at = last_activity_at
    return pool._entries[conv_id]


# ── Configuration (env-var fail-safe) ────────────────────────────────────────


def test_resolve_server_idle_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_OPENCODE_SERVER_IDLE_TIMEOUT_S", raising=False)
    assert resolve_server_idle_timeout_s() == 300.0


def test_resolve_server_idle_timeout_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_SERVER_IDLE_TIMEOUT_S", "0")
    assert resolve_server_idle_timeout_s() == 0.0


@pytest.mark.parametrize("bad", ["abc", "-1", ""])
def test_resolve_server_idle_timeout_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_SERVER_IDLE_TIMEOUT_S", bad)
    assert resolve_server_idle_timeout_s() == 300.0


def test_resolve_max_warm_servers_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_OPENCODE_MAX_WARM_SERVERS", raising=False)
    assert resolve_max_warm_servers() == 2


def test_resolve_max_warm_servers_zero_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # Zero is reserved for "explicitly disabled/unlimited" only when the
    # convention is later introduced. Today it falls back to the default
    # so a misconfiguration cannot silently allow unbounded overcommit.
    monkeypatch.setenv("OMNIGENT_OPENCODE_MAX_WARM_SERVERS", "0")
    assert resolve_max_warm_servers() == 2


def test_resolve_max_warm_servers_negative_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_MAX_WARM_SERVERS", "-5")
    assert resolve_max_warm_servers() == 2


@pytest.mark.parametrize("bad", ["abc", "1.5"])
def test_resolve_max_warm_servers_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_MAX_WARM_SERVERS", bad)
    assert resolve_max_warm_servers() == 2


def test_resolve_reaper_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_OPENCODE_REAPER_INTERVAL_S", raising=False)
    assert resolve_reaper_interval_s() == 30.0


def test_resolve_attach_idle_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_OPENCODE_ATTACH_IDLE_TIMEOUT_S", raising=False)
    assert resolve_attach_idle_timeout_s() == 120.0


def test_resolve_capacity_wait_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_OPENCODE_CAPACITY_WAIT_TIMEOUT_S", raising=False)
    assert resolve_capacity_wait_timeout_s() == 60.0


# ── Activity / idle clock ────────────────────────────────────────────────────


async def test_register_grants_full_grace_window() -> None:
    """A newly-registered runtime is not eligible for hibernation until
    one full idle window has elapsed."""
    adapter = _FakeAdapter()
    pool = _pool(adapter, idle_timeout_s=10.0, reaper_interval_s=0.001)
    now = 1000.0
    pool._clock = lambda: now
    pool.register("conv_a", _entry("conv_a", last_activity_at=now))
    # Advance one second and re-scan — the runtime just got a full grace
    # window from ``register`` (10 s), so 1 s of additional idle is well
    # below the cutoff.
    pool._clock = lambda: now + 1.0
    await pool._scan_once()
    assert adapter.hibernate_calls == []


async def test_activity_resets_idle_clock() -> None:
    """``mark_activity`` pushes the clock forward so the runtime
    survives the next scan even after the idle window has elapsed."""
    adapter = _FakeAdapter()
    pool = _pool(adapter, idle_timeout_s=5.0, reaper_interval_s=0.001)
    now = 1000.0
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 100)
    # Without an activity bump, the reaper would hibernate conv_a.
    await pool._scan_once()
    assert len(adapter.hibernate_calls) == 1

    adapter = _FakeAdapter()
    pool = _pool(adapter, idle_timeout_s=5.0, reaper_interval_s=0.001)
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 100)
    await pool.mark_activity("conv_a")
    pool._clock = lambda: now + 0.001
    await pool._scan_once()
    assert adapter.hibernate_calls == []


async def test_idle_runtime_becomes_hibernation_candidate() -> None:
    adapter = _FakeAdapter()
    pool = _pool(adapter, idle_timeout_s=10.0, reaper_interval_s=0.001)
    now = 1000.0
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 100)
    await pool._scan_once()
    assert ("conv_a", "idle") in adapter.hibernate_calls


async def test_busy_runtime_is_excluded() -> None:
    adapter = _FakeAdapter(busy={"conv_a"})
    pool = _pool(adapter, idle_timeout_s=10.0, reaper_interval_s=0.001)
    now = 1000.0
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 100)
    await pool._scan_once()
    assert adapter.hibernate_calls == []


async def test_attached_tmux_client_excludes_runtime() -> None:
    """The attach-pane reaper handles the attached case; the runtime
    pool still respects it as a busy signal so an attached session
    isn't killed by either layer."""
    adapter = _FakeAdapter(attached={"conv_a"})
    pool = _pool(adapter, idle_timeout_s=10.0, reaper_interval_s=0.001)
    now = 1000.0
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 100)
    await pool._scan_once()
    assert adapter.hibernate_calls == []


# ── Capacity / LRU selection ──────────────────────────────────────────────────


async def test_lru_selects_oldest_eligible_idle() -> None:
    adapter = _FakeAdapter(warm_count=2)
    pool = _pool(adapter, max_warm_servers=2, capacity_wait_timeout_s=0.0)
    now = 1000.0
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 5)
    _register_with_past_activity(pool, "conv_b", last_activity_at=now - 100)
    _register_with_past_activity(pool, "conv_c", last_activity_at=now - 50)
    # conv_c requests a wake; pool is full so LRU idle (conv_b) is evicted.
    candidate = pool._select_lru_idle_candidate("conv_c")
    assert candidate is not None
    assert candidate.conversation_id == "conv_b"


async def test_requester_is_never_evicted_as_capacity_victim() -> None:
    """Verify the pure selection predicate excludes the requester.

    ``_select_lru_idle_candidate`` is a pure helper; the requester
    should never appear in the candidate list, regardless of whether
    the requester already owns a slot.
    """
    adapter = _FakeAdapter(warm_count=2)
    pool = _pool(adapter, max_warm_servers=2, capacity_wait_timeout_s=0.0)
    now = 1000.0
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 5)
    _register_with_past_activity(pool, "conv_b", last_activity_at=now - 100)
    _register_with_past_activity(pool, "conv_c", last_activity_at=now - 50)
    # conv_b is the global LRU, but if conv_b itself is the requester it
    # is excluded from the candidate list so it never evicts itself.
    # After excluding conv_b, conv_c is the LRU (last_activity=now-50).
    candidate = pool._select_lru_idle_candidate("conv_b")
    assert candidate is not None
    assert candidate.conversation_id == "conv_c"
    assert candidate.conversation_id != "conv_b"


async def test_capacity_never_exceeds_max_after_enforcement() -> None:
    """After ``ensure_awake`` returns, the warm-server count is at most
    ``max_warm_servers`` and the requester owns one slot."""
    adapter = _FakeAdapter(warm_count=2)
    pool = _pool(adapter, max_warm_servers=2, capacity_wait_timeout_s=0.5)
    pool._clock = lambda: 0.0
    # Two warm entries already occupy the pool; conv_c is a brand-new
    # requester (no entry, no slot) and needs the pool to evict one LRU
    # idle runtime to free capacity.
    _register_with_past_activity(pool, "conv_a", last_activity_at=0.0)
    _register_with_past_activity(pool, "conv_b", last_activity_at=-100.0)
    await pool.ensure_awake("conv_c")
    # conv_b (LRU) was evicted; warm count went from 2 to 1.
    assert adapter.hibernate_calls == [("conv_b", "capacity")]
    assert adapter.warm_count() == 1


async def test_all_busy_capacity_returns_retryable_timeout() -> None:
    adapter = _FakeAdapter(warm_count=2, busy={"conv_a", "conv_b"})
    pool = _pool(adapter, max_warm_servers=2, capacity_wait_timeout_s=0.0)
    pool._clock = lambda: 0.0
    _register_with_past_activity(pool, "conv_a", last_activity_at=0.0)
    _register_with_past_activity(pool, "conv_b", last_activity_at=-1.0)
    # conv_c is a brand-new requester with no warm slot of its own.
    with pytest.raises(CapacityBusyError) as exc_info:
        await pool.ensure_awake("conv_c")
    # Operator/client-safe message (no internal paths, no PIDs).
    assert "OpenCode runtime capacity is full" in str(exc_info.value)
    # No eviction was attempted.
    assert adapter.hibernate_calls == []


async def test_capacity_wait_picks_up_freed_slot() -> None:
    """A bounded wait picks up capacity freed mid-wait."""
    adapter = _FakeAdapter(warm_count=2)
    pool = _pool(adapter, max_warm_servers=2, capacity_wait_timeout_s=1.0)
    pool._clock = lambda: 0.0
    _register_with_past_activity(pool, "conv_a", last_activity_at=0.0)
    _register_with_past_activity(pool, "conv_b", last_activity_at=0.0)
    # conv_c is a brand-new requester with no warm slot.

    async def _free_slot() -> None:
        await asyncio.sleep(0.05)
        # Simulate conv_a going idle; warm_count drops to 1.
        adapter._warm_count = 1

    task = asyncio.create_task(_free_slot())
    del task  # fire-and-forget: the bounded wait consumes the slot.
    await pool.ensure_awake("conv_c")
    # ensure_awake ran through the bounded wait without raising.
    assert any(call[0] == "conv_a" and call[1] == "capacity" for call in adapter.hibernate_calls)


# ── Lifecycle lock / race safety ──────────────────────────────────────────────


async def test_second_busy_check_prevents_select_to_reap_race() -> None:
    """The reaper picks conv_a as the LRU idle candidate, but the
    adapter reports it became busy between classification and teardown;
    the runtime must NOT be hibernated."""
    adapter = _FakeAdapter(warm_count=1)
    pool = _pool(adapter, idle_timeout_s=10.0, reaper_interval_s=0.001)
    now = 1000.0
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 100)
    # Flip adapter.is_busy between classification and teardown: first
    # call returns False (classification), second call returns True
    # (post-classification busy check inside the reaper).
    call_count = {"n": 0}

    async def _is_busy(conv_id: str) -> bool:
        call_count["n"] += 1
        return call_count["n"] >= 2  # second call sees a fresh busy state

    adapter.is_busy = _is_busy  # type: ignore[assignment]
    pool._clock = lambda: now
    await pool._scan_once()
    assert adapter.hibernate_calls == []


async def test_hibernation_lock_serializes_with_prompt_dispatch() -> None:
    """While a hibernate is in progress, a parallel prompt dispatch
    against the same conversation is blocked by the entry's lifecycle
    lock (the second busy check sees a state flip and aborts)."""
    adapter = _FakeAdapter(warm_count=1)
    pool = _pool(adapter, idle_timeout_s=10.0, reaper_interval_s=0.001)
    now = 1000.0
    pool._clock = lambda: now
    _register_with_past_activity(pool, "conv_a", last_activity_at=now - 100)

    # Pre-acquire the entry's lifecycle lock so a hibernate cannot
    # complete; this simulates an in-flight prompt dispatch against
    # conv_a.
    entry = pool._entries["conv_a"]
    await entry.lifecycle_lock.acquire()
    try:
        # Adapter's is_busy returns False (the prompt would normally be
        # allowed) but the lifecycle lock forces a hibernate abort.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.hibernate("conv_a", reason="idle"), timeout=0.02)
    finally:
        entry.lifecycle_lock.release()


# ── Reaper-loop resilience ───────────────────────────────────────────────────


async def test_reaper_exceptions_do_not_kill_loop() -> None:
    """A scan that raises must not stop the next scan."""
    calls = {"n": 0}

    async def _flaky_scan() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient scan failure")

    adapter = _FakeAdapter()
    pool = _pool(adapter, idle_timeout_s=10.0, reaper_interval_s=0.001)
    pool._scan_once = _flaky_scan  # type: ignore[assignment]
    await pool.start()
    try:
        # Two scans should complete: the first raises (caught), the
        # second succeeds.
        for _ in range(40):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await pool.shutdown()
    assert calls["n"] >= 2


async def test_reaper_disabled_when_idle_timeout_non_positive() -> None:
    adapter = _FakeAdapter(warm_count=1)
    pool = _pool(adapter, idle_timeout_s=0.0, reaper_interval_s=0.001)
    now = 1000.0
    pool.register("conv_a", _entry("conv_a", last_activity_at=now - 99999))
    pool._clock = lambda: now
    await pool._scan_once()
    assert adapter.hibernate_calls == []


# ── Diagnostic snapshot ──────────────────────────────────────────────────────


def test_snapshot_sanitized_for_operator_surface() -> None:
    adapter = _FakeAdapter(warm_count=1)
    pool = _pool(adapter)
    pool.register("conv_a", _entry("conv_a", last_activity_at=1000.0))
    pool._entries["conv_a"].adapter_metadata = {
        "server_pid": 1234,
        "attach_pid": 5678,
        "port": 4099,
        "auth_secret": "secret-must-not-leak",
        "opencode_json": {"model": "x"},
    }
    snapshot = pool.snapshot()
    assert len(snapshot) == 1
    row = snapshot[0]
    assert row["conversation_id"] == "conv_a"
    assert row["state"] == "awake"
    # Sanitized — secrets / generated config never appear.
    assert "auth_secret" not in row
    assert "opencode_json" not in row
    # Allowed diagnostic fields.
    assert row["server_pid"] == 1234
    assert row["attach_pid"] == 5678
    assert row["port"] == 4099


def test_snapshot_only_awake_filter() -> None:
    adapter = _FakeAdapter(warm_count=1)
    pool = _pool(adapter)
    pool.register("conv_a", _entry("conv_a", last_activity_at=1000.0))
    pool.register("conv_b", _entry("conv_b", last_activity_at=1000.0))
    pool._entries["conv_b"].state = LifecycleState.HIBERNATED
    only_awake = pool.snapshot(only_awake=True)
    assert {row["conversation_id"] for row in only_awake} == {"conv_a"}


# ── ensure_awake already-registered no-op ────────────────────────────────────


async def test_ensure_awake_returns_existing_when_already_awake() -> None:
    adapter = _FakeAdapter(warm_count=1)
    pool = _pool(adapter, max_warm_servers=2, capacity_wait_timeout_s=0.0)
    pool.register("conv_a", _entry("conv_a", last_activity_at=0.0))
    entry = await pool.ensure_awake("conv_a")
    assert entry is not None
    assert entry.conversation_id == "conv_a"
    assert adapter.hibernate_calls == []


# ── terminate frees capacity ────────────────────────────────────────────────


async def test_terminate_frees_capacity_slot() -> None:
    adapter = _FakeAdapter(warm_count=2)
    pool = _pool(adapter, max_warm_servers=2, capacity_wait_timeout_s=0.0)
    pool.register("conv_a", _entry("conv_a", last_activity_at=0.0))
    pool.register("conv_b", _entry("conv_b", last_activity_at=-100.0))
    await pool.terminate("conv_a", reason="delete")
    assert ("conv_a", "delete") in adapter.terminate_calls
    # Now there is 1 warm slot; conv_b can stay; conv_c can come in
    # without eviction.
    pool.register("conv_c", _entry("conv_c", last_activity_at=0.0))
    await pool.ensure_awake("conv_c")
    # No new hibernation should have been triggered.
    assert all(call[0] != "conv_b" for call in adapter.hibernate_calls)


async def test_shutdown_tears_down_every_owned_runtime() -> None:
    adapter = _FakeAdapter(warm_count=3)
    pool = _pool(adapter)
    pool.register("conv_a", _entry("conv_a", last_activity_at=0.0))
    pool.register("conv_b", _entry("conv_b", last_activity_at=0.0))
    pool.register("conv_c", _entry("conv_c", last_activity_at=0.0))
    await pool.shutdown()
    assert adapter.shutdown_calls == 1
    # Entry map is cleared on shutdown.
    assert pool.has_entry("conv_a") is False
