import json

import pytest

from omnigent.benchmark_capture_native import begin_native, close_native, finish_native
from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState, write_bridge_state
from tests.test_benchmark_capture import _init_repo


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted", "closed"])
def test_native_hook_forwarder_identity_and_cleanup(tmp_path, monkeypatch, terminal):
    repo = tmp_path / "repo"
    _init_repo(repo)
    root = tmp_path / "captures"
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE_DIR", str(root))
    bridge = tmp_path / "bridge"
    home = bridge / "codex-home"
    write_bridge_state(
        bridge,
        CodexNativeBridgeState(
            session_id="session", socket_path="fixture", thread_id="thread", codex_home=str(home)
        ),
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "turn_id": "turn",
        "cwd": str(repo),
        "prompt": "Build it",
    }
    begin_native(bridge, payload)
    begin_native(bridge, payload)
    captures = list(root.glob("*/manifest.json"))
    assert len(captures) == 1
    before = json.loads(captures[0].read_text())
    assert before["start"] is not None
    assert before["completed_at"] is None
    (repo / "tracked.txt").write_text("native result\n")
    rollout = home / "sessions/2026/09/15/rollout-2026-09-15T00-00-00-thread.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread"}}) + "\n")
    # A stale or another session's notification must not finalize this capture.
    finish_native("other", "turn/completed", {"threadId": "thread", "turn": {"id": "turn"}})
    finish_native("session", "turn/completed", {"threadId": "thread", "turn": {"id": "stale"}})
    finish_native(
        "session", "turn/failed", {"willRetry": True, "threadId": "thread", "turn": {"id": "turn"}}
    )
    assert json.loads(captures[0].read_text())["completed_at"] is None
    if terminal == "closed":
        close_native(bridge)
    else:
        finish_native(
            "session",
            "turn/completed",
            {"threadId": "thread", "turn": {"id": "turn", "status": terminal}},
        )
    manifest = json.loads(captures[0].read_text())
    assert (
        manifest["terminal_state"]
        == {
            "completed": "completed",
            "failed": "failed",
            "interrupted": "cancelled",
            "closed": "failed",
        }[terminal]
    )
    assert manifest["end"] is not None
    assert manifest["native_rollout"]["status"] == "preserved"
    assert manifest["capture_id"] == before["capture_id"]
    finish_native(
        "session",
        "turn/completed",
        {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}},
    )
    assert json.loads(captures[0].read_text()) == manifest


def test_native_capture_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIGENT_BENCHMARK_CAPTURE", raising=False)
    begin_native(tmp_path, {"hook_event_name": "UserPromptSubmit"})
    finish_native("session", "turn/completed", {})
    close_native(tmp_path)
    assert list(tmp_path.iterdir()) == []
