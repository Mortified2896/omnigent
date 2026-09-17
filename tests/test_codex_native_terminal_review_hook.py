from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.codex_native import bridge
from omnigent.harnesses.codex_native import terminal_review_hook as hook


def _state(*, turn_id: str | None) -> bridge.CodexNativeBridgeState:
    return bridge.CodexNativeBridgeState(
        session_id="session-1",
        socket_path="/tmp/app-server.sock",
        thread_id="thread-primary",
        codex_home="/tmp/codex-home",
        active_turn_id=turn_id,
        cwd="/tmp/workspace",
    )


def test_matching_terminal_clear_schedules_exact_primary_turn(tmp_path: Path, monkeypatch) -> None:
    hook.uninstall_self_review_hook_for_tests()
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(hook, "_schedule_review", lambda **kwargs: scheduled.append(kwargs))
    hook.install_self_review_hook()
    bridge.write_bridge_state(tmp_path, _state(turn_id="turn-primary"))

    assert bridge.clear_active_turn_id_if_matches(tmp_path, "turn-primary") is True
    assert bridge.read_bridge_state(tmp_path).active_turn_id is None  # type: ignore[union-attr]
    assert scheduled == [
        {
            "bridge_dir": tmp_path,
            "session_id": "session-1",
            "parent_thread_id": "thread-primary",
            "primary_turn_id": "turn-primary",
        }
    ]

    hook.uninstall_self_review_hook_for_tests()
    hook.install_self_review_hook()


def test_stale_or_ambiguous_terminal_edge_does_not_schedule(tmp_path: Path, monkeypatch) -> None:
    hook.uninstall_self_review_hook_for_tests()
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(hook, "_schedule_review", lambda **kwargs: scheduled.append(kwargs))
    hook.install_self_review_hook()
    bridge.write_bridge_state(tmp_path, _state(turn_id="turn-newer"))

    assert bridge.clear_active_turn_id_if_matches(tmp_path, "turn-stale") is False
    assert bridge.clear_active_turn_id_if_matches(tmp_path, None) is False
    assert bridge.read_bridge_state(tmp_path).active_turn_id == "turn-newer"  # type: ignore[union-attr]
    assert scheduled == []

    hook.uninstall_self_review_hook_for_tests()
    hook.install_self_review_hook()
