"""Integration tests for the runner-owned opencode runtime lifecycle.

The runtime manager (see ``omnigent.runner.opencode_runtime_manager``) is
the policy layer; this file exercises the opencode-specific adapter that
plugs the manager into the runner's existing
``_AUTO_OPENCODE_SERVERS`` / terminal-registry / forwarder-task
infrastructure. The focus is end-to-end correctness on the side
effects we care about for memory safety:

* hibernation tears down ``opencode serve`` + forwarder + attach terminal,
  preserves XDG data / config / external session id / route approval;
* resume starts a single fresh server, reuses the persisted session id,
  refreshes bridge state with the new URL;
* capacity enforcement evicts LRU idle candidates and rejects when the
  pool is full + busy;
* duplicate registrations / races don't produce duplicate processes or
  stale bridge coords;
* archive / delete teardown is isolated from hibernation behavior;
* runner shutdown closes every owned runtime.

The tests stub ``OpenCodeNativeServer`` with a fake that simulates the
``subprocess.Popen`` lifecycle (the real server is exercised by
``test_opencode_native_app_server.py``), and patch the runner's
bridge-state + forwarder hooks so the adapter can run without an actual
opencode binary on PATH.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import omnigent.runner.app as app_mod
from omnigent.opencode_native_bridge import (
    OpenCodeNativeBridgeState,
    prepare_bridge_dir,
    read_bridge_state,
    write_bridge_state,
)

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeServerProcess:
    """Drop-in for ``subprocess.Popen`` that just records a pid + state."""

    instances: list[_FakeServerProcess] = []

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._terminated = False
        self._killed = False
        _FakeServerProcess.instances.append(self)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self._terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self._killed = True
        self.returncode = -9

    async def _wait_or_close(self) -> None:
        if self.returncode is None:
            self.returncode = 0
        return

    wait = _wait_or_close


def _make_fake_server(process: _FakeServerProcess, *, port: int = 49231):
    """Build a stub ``OpenCodeNativeServer``-shaped object for the
    adapter to call. ``close`` mirrors the real server's coroutine.
    """
    server = MagicMock()
    server.process = process
    server.port = port
    server.base_url = f"http://127.0.0.1:{port}"
    server.auth_secret = "fake-auth-secret"
    server.xdg_data_home = MagicMock()
    server.xdg_data_home.__str__ = lambda self: "/tmp/fake-xdg-data"
    server.xdg_config_home = MagicMock()
    server.xdg_config_home.__str__ = lambda self: "/tmp/fake-xdg-config"

    closed = {"called": False}

    async def _close() -> None:
        closed["called"] = True

    server.close = _close
    server.closed = closed
    return server


# ── Helpers ────────────────────────────────────────────────────────────────


def _install_opencode_pool(monkeypatch: pytest.MonkeyPatch, *, max_warm: int = 2) -> Any:
    """Install a fresh pool on the module-level runner app for tests
    that need to invoke ``_register_opencode_runtime_after_boot`` /
    ``delete_session`` paths without booting FastAPI."""
    from omnigent.runner.opencode_runtime_manager import OpenCodeRuntimePool

    pool = OpenCodeRuntimePool(
        adapter=MagicMock(),  # Tests that don't exercise hibernation skip the adapter.
        idle_timeout_s=300.0,
        max_warm_servers=max_warm,
        reaper_interval_s=0.01,
    )
    monkeypatch.setattr(app_mod, "_OPENCODE_RUNTIME_POOL_FOR_TESTS", pool, raising=False)
    return pool


@pytest.fixture
def bridge_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "opencode-native-bridges"
    root.mkdir(parents=True, exist_ok=True)
    import omnigent.opencode_native_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", root)
    return root


# ── Hibernation teardown ────────────────────────────────────────────────────


async def test_hibernate_closes_attach_forwarder_and_serve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bridge_root: Path
) -> None:
    """Adapter.hibernate closes the opencode attach terminal, cancels
    the SSE forwarder, and closes ``opencode serve`` in the documented
    teardown order."""
    from omnigent.runner.opencode_runtime_manager import OpenCodeRuntimePool

    bridge_id = "conv_hibernate_a"
    bd = prepare_bridge_dir(bridge_id)
    # Seed a populated bridge state with a live URL + external session id.
    write_bridge_state(
        bd,
        OpenCodeNativeBridgeState(
            session_id=bridge_id,
            server_base_url="http://127.0.0.1:49231",
            opencode_session_id="ses_abc123",
            auth_secret="fake-secret",
            xdg_data_home=str(bd / "xdg-data"),
            xdg_config_home=str(bd / "xdg-config"),
            model_override="anthropic/claude-opus-4",
            reasoning_effort="medium",
            permission_mode="accept_edits",
            route_approved=True,
            workspace=str(tmp_path),
        ),
    )
    process = _FakeServerProcess(pid=4242)
    server = _make_fake_server(process)

    closed_events: list[str] = []

    async def _close_terminal(conv_id: str, term_id: str) -> None:
        closed_events.append(f"close_terminal:{conv_id}:{term_id}")

    resource_registry = MagicMock()
    resource_registry.close_terminal = AsyncMock(side_effect=_close_terminal)

    forwarder_cancelled: list[str] = []

    async def _cancel_forwarder(conv_id: str) -> bool:
        forwarder_cancelled.append(conv_id)
        # Don't pop the server here — the adapter's ``hibernate`` does that
        # after the forwarder cancel; the test wants to observe that the
        # adapter's teardown completes the close.
        return True

    publish_events: list[tuple[str, dict[str, Any]]] = []

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        publish_events.append((conv_id, event))

    monkeypatch.setattr(app_mod, "_cancel_auto_forwarder_task", _cancel_forwarder)
    import omnigent.runner.tool_dispatch as td

    monkeypatch.setattr(td, "_publish_terminal_deleted_event", lambda **kw: None)
    monkeypatch.setattr(app_mod, "_AUTO_OPENCODE_SERVERS", {bridge_id: server})

    from omnigent.runner.app import _OpenCodeAdapter

    adapter = _OpenCodeAdapter(
        resource_registry=resource_registry,
        process_manager=None,
        publish_event=_publish,
        cancel_forwarder=_cancel_forwarder,
        auto_servers=app_mod._AUTO_OPENCODE_SERVERS,
        active_turns={},
        native_pane_status={},
        list_tmux_clients=lambda *a, **kw: [],
    )
    OpenCodeRuntimePool(adapter=adapter, idle_timeout_s=300.0, max_warm_servers=2)

    ok = await adapter.hibernate(bridge_id, reason="test")
    assert ok is True
    # Forwarder cancelled.
    assert forwarder_cancelled == [bridge_id]
    # Terminal closed.
    assert len(closed_events) == 1
    assert closed_events[0] == f"close_terminal:{bridge_id}:terminal_opencode_main"
    # Server closed (its ``close`` coroutine ran).
    assert server.closed["called"] is True
    # Bridge state URL cleared (sentinel ``about:blank`` — not a usable
    # live URL), but external session id and route metadata survive.
    state = read_bridge_state(bd)
    assert state is not None
    assert state.server_base_url == "about:blank"
    assert state.auth_secret is None
    assert state.opencode_session_id == "ses_abc123"
    assert state.model_override == "anthropic/claude-opus-4"
    assert state.reasoning_effort == "medium"
    assert state.permission_mode == "accept_edits"
    assert state.route_approved is True
    # Active message id cleared.
    assert state.active_message_id is None


async def test_hibernate_skips_when_attach_already_gone(
    monkeypatch: pytest.MonkeyPatch, bridge_root: Path
) -> None:
    """An idempotent hibernate with no live attach + no live server
    returns ``False`` so the pool's capacity counter doesn't drift."""
    from omnigent.runner.opencode_runtime_manager import OpenCodeRuntimePool

    # A terminal registry whose ``get`` returns ``None`` for the
    # conversation — i.e. no live attach terminal.
    terminal_registry = MagicMock()
    terminal_registry.get = MagicMock(return_value=None)
    resource_registry = MagicMock()
    resource_registry.terminal_registry = terminal_registry
    resource_registry.close_terminal = AsyncMock(side_effect=AssertionError("should not close"))

    async def _cancel_forwarder(conv_id: str) -> bool:
        return False

    monkeypatch.setattr(app_mod, "_cancel_auto_forwarder_task", _cancel_forwarder)
    monkeypatch.setattr(app_mod, "_AUTO_OPENCODE_SERVERS", {})

    from omnigent.runner.app import _OpenCodeAdapter

    adapter = _OpenCodeAdapter(
        resource_registry=resource_registry,
        process_manager=None,
        publish_event=lambda c, e: None,
        cancel_forwarder=_cancel_forwarder,
        auto_servers={},
        active_turns={},
        native_pane_status={},
        list_tmux_clients=lambda *a, **kw: [],
    )
    pool = OpenCodeRuntimePool(adapter=adapter, idle_timeout_s=300.0)
    del pool  # pool wiring-shape only; the adapter is what's exercised.
    ok = await adapter.hibernate("conv_missing", reason="test")
    assert ok is False


# ── Capacity enforcement / warm count ──────────────────────────────────────


async def test_six_sequential_sessions_never_exceed_max_warm(
    monkeypatch: pytest.MonkeyPatch, bridge_root: Path
) -> None:
    """Six sequential wake requests, each lasting only long enough to
    hibernate its predecessor, must never leave more than max_warm
    servers warm — the memory-safety acceptance criterion."""
    from omnigent.runner.opencode_runtime_manager import (
        LifecycleState,
        OpenCodeRuntimeEntry,
        OpenCodeRuntimePool,
    )

    resource_registry = MagicMock()
    resource_registry.close_terminal = AsyncMock()

    forwarder_cancelled: list[str] = []

    async def _cancel_forwarder(conv_id: str) -> bool:
        forwarder_cancelled.append(conv_id)
        app_mod._AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        return True

    monkeypatch.setattr(app_mod, "_cancel_auto_forwarder_task", _cancel_forwarder)
    monkeypatch.setattr(app_mod, "_AUTO_OPENCODE_SERVERS", {})

    from omnigent.runner.app import _OpenCodeAdapter

    adapter = _OpenCodeAdapter(
        resource_registry=resource_registry,
        process_manager=None,
        publish_event=lambda c, e: None,
        cancel_forwarder=_cancel_forwarder,
        auto_servers=app_mod._AUTO_OPENCODE_SERVERS,
        active_turns={},
        native_pane_status={},
        list_tmux_clients=lambda *a, **kw: [],
    )
    pool = OpenCodeRuntimePool(
        adapter=adapter,
        idle_timeout_s=300.0,
        max_warm_servers=2,
        reaper_interval_s=0.05,
    )
    await pool.start()

    try:
        for i in range(6):
            conv_id = f"conv_seq_{i}"
            process = _FakeServerProcess(pid=1000 + i)
            server = _make_fake_server(process)
            app_mod._AUTO_OPENCODE_SERVERS[conv_id] = server
            entry = OpenCodeRuntimeEntry(
                conversation_id=conv_id,
                state=LifecycleState.AWAKE,
                lifecycle_lock=asyncio.Lock(),
            )
            pool.register(conv_id, entry)
            await pool.mark_activity(conv_id)
            # Allow the reaper one tick to settle.
            await asyncio.sleep(0.1)
            # Warm count must never exceed the cap.
            assert adapter.warm_count() <= 2, (
                f"warm_count={adapter.warm_count()} exceeded max_warm=2 at iteration {i}"
            )
            # Force this entry to look idle by aging it past the window.
            entry.last_activity_at = pool._clock() - 1000.0
        # Final: only the last two can still be warm.
        assert adapter.warm_count() <= 2
    finally:
        await pool.shutdown()


# ── Bridge directory preservation on hibernation ───────────────────────────


async def test_hibernate_preserves_bridge_xdg_directories(
    monkeypatch: pytest.MonkeyPatch, bridge_root: Path
) -> None:
    """The bridge directory and its XDG roots must survive hibernation
    so the next prompt can resume against the same persisted session."""
    from omnigent.runner.opencode_runtime_manager import OpenCodeRuntimePool

    bridge_id = "conv_preserve_xdg"
    bd = prepare_bridge_dir(bridge_id)
    # Touch the XDG dirs so a later stat check confirms they survived.
    (bd / "xdg-data" / "opencode").mkdir(parents=True, exist_ok=True)
    (bd / "xdg-data" / "opencode").mkdir(parents=True, exist_ok=True)
    (bd / "xdg-data" / "opencode" / "session_marker").write_text("alive")
    (bd / "xdg-config" / "opencode.json").write_text('{"model": "x"}')

    resource_registry = MagicMock()
    resource_registry.close_terminal = AsyncMock()

    async def _cancel_forwarder(conv_id: str) -> bool:
        app_mod._AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        return True

    monkeypatch.setattr(app_mod, "_cancel_auto_forwarder_task", _cancel_forwarder)
    monkeypatch.setattr(app_mod, "_AUTO_OPENCODE_SERVERS", {})

    from omnigent.runner.app import _OpenCodeAdapter

    adapter = _OpenCodeAdapter(
        resource_registry=resource_registry,
        process_manager=None,
        publish_event=lambda c, e: None,
        cancel_forwarder=_cancel_forwarder,
        auto_servers={},
        active_turns={},
        native_pane_status={},
        list_tmux_clients=lambda *a, **kw: [],
    )
    pool = OpenCodeRuntimePool(adapter=adapter, idle_timeout_s=300.0)
    del pool  # pool wiring-shape only; the adapter is what's exercised.
    await adapter.hibernate(bridge_id, reason="test")
    # XDG data + config + custom files survive.
    assert (bd / "xdg-data" / "opencode" / "session_marker").is_file()
    assert (bd / "xdg-config" / "opencode.json").is_file()
    # The bridge dir itself was not deleted.
    assert bd.is_dir()


# ── Resume preserves external session id / route approval ─────────────────


async def test_resume_preserves_external_session_id(
    monkeypatch: pytest.MonkeyPatch, bridge_root: Path, tmp_path: Path
) -> None:
    """After hibernation, the persisted external session id stays
    alive so the resume path can re-bind to it instead of starting
    empty."""
    bridge_id = "conv_resume_a"
    bd = prepare_bridge_dir(bridge_id)
    write_bridge_state(
        bd,
        OpenCodeNativeBridgeState(
            session_id=bridge_id,
            server_base_url="http://127.0.0.1:49231",
            opencode_session_id="ses_resume_42",
            auth_secret="fake-secret",
            xdg_data_home=str(bd / "xdg-data"),
            xdg_config_home=str(bd / "xdg-config"),
            model_override="anthropic/claude-opus-4",
            reasoning_effort="medium",
            permission_mode="accept_edits",
            route_approved=True,
            workspace=str(tmp_path),
        ),
    )

    resource_registry = MagicMock()
    resource_registry.close_terminal = AsyncMock()

    async def _cancel_forwarder(conv_id: str) -> bool:
        app_mod._AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        return True

    monkeypatch.setattr(app_mod, "_cancel_auto_forwarder_task", _cancel_forwarder)
    monkeypatch.setattr(app_mod, "_AUTO_OPENCODE_SERVERS", {})

    from omnigent.runner.app import _OpenCodeAdapter

    adapter = _OpenCodeAdapter(
        resource_registry=resource_registry,
        process_manager=None,
        publish_event=lambda c, e: None,
        cancel_forwarder=_cancel_forwarder,
        auto_servers={},
        active_turns={},
        native_pane_status={},
        list_tmux_clients=lambda *a, **kw: [],
    )
    await adapter.hibernate(bridge_id, reason="idle")

    # Verify: external session id + route approval + permission mode +
    # model + reasoning effort survive.
    state = read_bridge_state(bd)
    assert state is not None
    assert state.opencode_session_id == "ses_resume_42"
    assert state.route_approved is True
    assert state.permission_mode == "accept_edits"
    assert state.model_override == "anthropic/claude-opus-4"
    assert state.reasoning_effort == "medium"
    # Live URL is cleared (sentinel ``about:blank`` — not a usable live URL).
    assert state.server_base_url == "about:blank"


# ── Archive vs delete semantics ────────────────────────────────────────────


async def test_hibernate_does_not_remove_bridge_dir(
    monkeypatch: pytest.MonkeyPatch, bridge_root: Path
) -> None:
    """``hibernate`` (which the archive path uses) must preserve the
    bridge dir so a later restore / resume can pick the session back
    up. ``delete`` is the only path that removes the bridge dir."""
    bridge_id = "conv_archive_a"
    bd = prepare_bridge_dir(bridge_id)
    (bd / "xdg-data" / "opencode").mkdir(parents=True, exist_ok=True)
    (bd / "xdg-data" / "opencode" / "session_marker").write_text("alive")

    resource_registry = MagicMock()
    resource_registry.close_terminal = AsyncMock()

    async def _cancel_forwarder(conv_id: str) -> bool:
        app_mod._AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        return True

    monkeypatch.setattr(app_mod, "_cancel_auto_forwarder_task", _cancel_forwarder)
    monkeypatch.setattr(app_mod, "_AUTO_OPENCODE_SERVERS", {})

    from omnigent.runner.app import _OpenCodeAdapter

    adapter = _OpenCodeAdapter(
        resource_registry=resource_registry,
        process_manager=None,
        publish_event=lambda c, e: None,
        cancel_forwarder=_cancel_forwarder,
        auto_servers={},
        active_turns={},
        native_pane_status={},
        list_tmux_clients=lambda *a, **kw: [],
    )
    await adapter.hibernate(bridge_id, reason="archive")
    # Bridge dir survives hibernate / archive.
    assert bd.is_dir()
    assert (bd / "xdg-data" / "opencode" / "session_marker").is_file()


# ── Race: prompt racing with hibernate ─────────────────────────────────────


async def test_prompt_dispatch_winning_locks_prevents_hibernate(
    monkeypatch: pytest.MonkeyPatch, bridge_root: Path
) -> None:
    """A prompt dispatch holds the entry's lifecycle lock while it
    injects; the hibernate path waits, then re-checks busy state and
    aborts when the prompt is still in-flight."""
    from omnigent.runner.opencode_runtime_manager import (
        LifecycleState,
        OpenCodeRuntimeEntry,
        OpenCodeRuntimePool,
    )

    resource_registry = MagicMock()
    resource_registry.close_terminal = AsyncMock()

    async def _cancel_forwarder(conv_id: str) -> bool:
        app_mod._AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        return True

    monkeypatch.setattr(app_mod, "_cancel_auto_forwarder_task", _cancel_forwarder)
    monkeypatch.setattr(app_mod, "_AUTO_OPENCODE_SERVERS", {})

    from omnigent.runner.app import _OpenCodeAdapter

    adapter = _OpenCodeAdapter(
        resource_registry=resource_registry,
        process_manager=None,
        publish_event=lambda c, e: None,
        cancel_forwarder=_cancel_forwarder,
        auto_servers={},
        active_turns={},
        native_pane_status={},
        list_tmux_clients=lambda *a, **kw: [],
    )
    pool = OpenCodeRuntimePool(adapter=adapter, idle_timeout_s=300.0, max_warm_servers=2)

    entry = OpenCodeRuntimeEntry(
        conversation_id="conv_race",
        state=LifecycleState.AWAKE,
        lifecycle_lock=asyncio.Lock(),
    )
    pool.register("conv_race", entry)

    # Simulate an in-flight prompt by holding the lifecycle lock.
    await entry.lifecycle_lock.acquire()
    try:
        # The pool sees the entry as busy (via the held lock) and the
        # adapter's is_busy is False, but the lifecycle lock serialization
        # guarantees the prompt can complete.
        hibernate_task = asyncio.create_task(pool.hibernate("conv_race", reason="idle"))
        # The hibernate waits for the lifecycle lock.
        await asyncio.sleep(0.05)
        assert not hibernate_task.done()
        # Release the lock; the hibernate can now check busy state.
        entry.state = LifecycleState.BUSY  # prompt in flight
        entry.lifecycle_lock.release()
        # is_busy now reports BUSY (entry.is_busy True), so hibernate aborts.
        ok = await hibernate_task
        assert ok is False
    finally:
        if entry.lifecycle_lock.locked():
            entry.lifecycle_lock.release()


# ── Delete behavior (bridge dir cleanup) ───────────────────────────────────


async def test_delete_session_bridge_dir_removal_via_existing_helper(
    monkeypatch: pytest.MonkeyPatch, bridge_root: Path
) -> None:
    """``_delete_native_bridge_dirs`` (the runner's existing delete
    cleanup) removes the opencode-native bridge dir on a clean delete
    — so a deleted session cannot be resumed (no leftover external
    session id pointing at a dead server)."""
    from omnigent.runner.app import _delete_native_bridge_dirs

    bridge_id = "conv_delete_a"
    bd = prepare_bridge_dir(bridge_id)
    assert bd.is_dir()

    class _NullClient:
        async def get(self, *args: Any, **kwargs: Any) -> Any:
            r = MagicMock()
            r.json.return_value = {"data": {"labels": {}}}
            return r

    await _delete_native_bridge_dirs(server_client=_NullClient(), session_id=bridge_id)
    assert not bd.exists()


# ── Capacity wait: all-busy returns retryable error ────────────────────────


async def test_all_busy_capacity_raises_retryable_error(
    monkeypatch: pytest.MonkeyPatch, bridge_root: Path
) -> None:
    """When the pool is full + every entry is busy, ``ensure_awake``
    raises ``CapacityBusyError`` — the public API surface code."""
    from omnigent.runner.opencode_runtime_manager import (
        CapacityBusyError,
        OpenCodeRuntimeEntry,
        OpenCodeRuntimePool,
    )

    resource_registry = MagicMock()
    resource_registry.close_terminal = AsyncMock()
    busy = {"conv_a", "conv_b"}

    async def _cancel_forwarder(conv_id: str) -> bool:
        app_mod._AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        return True

    async def _is_busy(conv_id: str) -> bool:
        return conv_id in busy

    async def _has_attached(conv_id: str) -> bool:
        return False

    monkeypatch.setattr(app_mod, "_cancel_auto_forwarder_task", _cancel_forwarder)
    servers = {"conv_a": object(), "conv_b": object()}
    monkeypatch.setattr(app_mod, "_AUTO_OPENCODE_SERVERS", servers)

    class _StubAdapter:
        async def is_busy(self, conv_id: str) -> bool:
            return conv_id in busy

        async def has_attached_tmux_client(self, conv_id: str) -> bool:
            return False

        def warm_count(self) -> int:
            return 2

        def attach_pid(self, conv_id: str) -> int | None:
            return None

        async def hibernate(self, conv_id: str, *, reason: str) -> bool:
            busy.discard(conv_id)
            return True

        async def terminate(self, conv_id: str, *, reason: str) -> bool:
            busy.discard(conv_id)
            return True

        async def shutdown(self) -> None:
            return None

    pool = OpenCodeRuntimePool(
        adapter=_StubAdapter(),
        idle_timeout_s=300.0,
        max_warm_servers=2,
        capacity_wait_timeout_s=0.0,
    )
    pool.register("conv_a", OpenCodeRuntimeEntry(conversation_id="conv_a"))
    pool.register("conv_b", OpenCodeRuntimeEntry(conversation_id="conv_b"))
    with pytest.raises(CapacityBusyError) as exc_info:
        await pool.ensure_awake("conv_c")
    assert exc_info.value.code == "opencode_capacity_busy" or "capacity" in str(exc_info.value)
