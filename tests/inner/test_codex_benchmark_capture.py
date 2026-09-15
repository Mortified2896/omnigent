"""Isolated turn acceptance through the real app-server event loop, without an LLM."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from omnigent.benchmark_capture import capture_io
from omnigent.benchmark_capture_export import export_candidate
from omnigent.inner.codex_executor import CodexExecutor, _CodexAppServerSession
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete
from tests.test_benchmark_capture import _git, _init_repo


@pytest.mark.parametrize(
    "outcome", ["completed", "failed", "cancelled", "exception", "final_item"]
)
async def test_real_dispatch_snapshot_order_and_terminal_paths(tmp_path, monkeypatch, outcome):
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    root = tmp_path / "captures"
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE_DIR", str(root))
    home = tmp_path / "codex-home"
    source = home / "sessions/2026/09/15/rollout-2026-09-15T00-00-00-native-thread.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "native-thread", "cli_version": "fixture"}}
        )
        + "\n"
    )
    session = _CodexAppServerSession(
        codex_path="fixture", cwd=str(repo), env={}, tool_executor=None
    )
    session._codex_home_dir = home
    session._proc = object()
    session.start = AsyncMock()

    async def request(method, params):
        if method == "thread/start":
            manifest = json.loads(next(root.glob("*/manifest.json")).read_text())
            assert manifest["start"]["head"] == head
            assert (repo / "tracked.txt").read_text() == "base\n"
            return {"result": {"thread": {"id": "native-thread"}}}
        if method == "thread/settings/update":
            return {"result": {}}
        assert method == "turn/start"
        (repo / "tracked.txt").write_text("mutated\n")
        with source.open("a") as stream:
            for record in [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "native-turn"},
                },
                {
                    "type": "turn_context",
                    "payload": {"turn_id": "native-turn", "model": "reported", "effort": "high"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "native-turn"},
                },
            ]:
                if outcome != "final_item" or record["payload"].get("type") != "task_complete":
                    stream.write(json.dumps(record) + "\n")
        if outcome == "exception":
            raise RuntimeError("provider transport failed")
        if outcome == "final_item":
            session._events.put_nowait(
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "native-turn",
                        "item": {
                            "id": "final-id",
                            "type": "agentMessage",
                            "text": "done",
                            "phase": "final_answer",
                        },
                    },
                }
            )
            return {"result": {"turn": {"id": "native-turn"}}}
        status = {"completed": "completed", "failed": "failed", "cancelled": "interrupted"}[
            outcome
        ]
        session._events.put_nowait(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "native-thread",
                    "turnId": "native-turn",
                    "turn": {"id": "native-turn", "status": status},
                },
            }
        )
        return {"result": {"turn": {"id": "native-turn"}}}

    session._request = request
    executor = CodexExecutor(model="requested", cwd=str(repo))
    executor._ensure_app_session = AsyncMock(return_value=session)
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "Implement feature", "session_id": "omni-session"}],
            [],
            "Instructions",
            ExecutorConfig(
                model="requested", extra={"omnigent_turn_id": "resp_1", "reasoning_effort": "high"}
            ),
        )
    ]
    manifest_path = next(root.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["omnigent_turn_id"] == "resp_1"
    assert manifest["omnigent_session_id"] == "omni-session"
    assert manifest["terminal_state"] == (
        {"exception": "failed", "final_item": "completed"}.get(outcome, outcome)
    )
    assert manifest["codex_thread_id"] == "native-thread"
    assert manifest["requested_model"] == "requested"
    assert manifest["observed_model"] == (None if outcome == "exception" else "reported")
    assert all(value is None for value in manifest["routing"].values())
    assert b"+mutated" in (manifest_path.parent / "end/unstaged.patch").read_bytes()
    assert (manifest_path.parent / "start/unstaged.patch").read_bytes() == b""
    if outcome in {"completed", "final_item"}:
        assert isinstance(events[-1], TurnComplete)
        assert manifest["native_rollout"]["boundary_status"] == (
            "incomplete" if outcome == "final_item" else "explicit"
        )
        candidate = export_candidate(manifest_path.parent, tmp_path / "export")
        assert candidate["dirty_end"] is True
        # Reconstruct the start and final tracked states in an independent checkout.
        restored = tmp_path / "restored"
        _git(tmp_path, "clone", str(repo), str(restored))
        assert (restored / "tracked.txt").read_text() == "base\n"
        _git(
            restored,
            "apply",
            "-",
            input_bytes=(manifest_path.parent / "end/unstaged.patch").read_bytes(),
        )
        assert (restored / "tracked.txt").read_text() == "mutated\n"
    else:
        assert isinstance(events[-1], ExecutorError)


async def test_capture_io_fault_does_not_fail_task():
    def broken():
        raise OSError("disk full")

    assert await capture_io(broken) is None


async def test_cancelled_executor_finalizes_before_cleanup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    root = tmp_path / "captures"
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE_DIR", str(root))
    started = asyncio.Event()

    class Session:
        async def run_turn(self, **kwargs):
            self.capture = kwargs["capture"]
            (repo / "tracked.txt").write_text("partial work\n")
            started.set()
            await asyncio.Event().wait()
            yield TurnComplete(response="unreachable")

        async def close(self):
            await capture_io(self.capture.finish, "cancelled")

    executor = CodexExecutor(model="requested", cwd=str(repo))
    executor._ensure_app_session = AsyncMock(return_value=Session())

    async def run():
        return [
            event
            async for event in executor.run_turn([{"role": "user", "content": "work"}], [], "")
        ]

    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    manifest = json.loads(next(root.glob("*/manifest.json")).read_text())
    assert manifest["terminal_state"] == "cancelled"
    assert manifest["end"] is not None


async def test_snapshot_failure_still_completes_codex(tmp_path, monkeypatch):
    from omnigent import benchmark_capture

    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE_DIR", str(tmp_path / "captures"))

    def broken(*args):
        raise OSError("snapshot failed")

    monkeypatch.setattr(benchmark_capture, "snapshot_git_state", broken)

    class Session:
        async def run_turn(self, **kwargs):
            yield TurnComplete(response="done")

    executor = CodexExecutor(model="requested", cwd=str(repo))
    executor._ensure_app_session = AsyncMock(return_value=Session())
    events = [
        event async for event in executor.run_turn([{"role": "user", "content": "work"}], [], "")
    ]
    assert events[-1].response == "done"
    manifest = json.loads(next((tmp_path / "captures").glob("*/manifest.json")).read_text())
    assert {error["stage"] for error in manifest["errors"]} >= {"start", "end"}
