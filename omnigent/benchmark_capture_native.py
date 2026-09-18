"""Bridge native pre-prompt hooks to terminal app-server notifications."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from omnigent.benchmark_capture import TurnCapture, _git_text, capture_enabled, capture_root
from omnigent.harnesses.codex_native.bridge import read_bridge_state


def _key(session_id: str, thread_id: str, turn_id: str) -> str:
    return hashlib.sha256(json.dumps([session_id, thread_id, turn_id]).encode()).hexdigest()


def begin_native(bridge_dir: Path, payload: dict[str, Any]) -> None:
    """Run inside UserPromptSubmit, before Codex may execute workspace tools."""
    if not capture_enabled() or payload.get("hook_event_name") != "UserPromptSubmit":
        return
    try:
        state = read_bridge_state(bridge_dir)
        turn_id, cwd = payload.get("turn_id"), payload.get("cwd")
        if (
            state is None
            or not isinstance(turn_id, str)
            or not turn_id
            or not isinstance(cwd, str)
            or not cwd
        ):
            return
        if payload.get("session_id") and payload["session_id"] != state.thread_id:
            return
        repo = Path(_git_text(Path(cwd), "rev-parse", "--show-toplevel") or cwd).resolve()
        root = capture_root()
        if root.is_relative_to(repo):
            return
        index = root / ".native-index"
        index.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = index / _key(state.session_id, state.thread_id, turn_id)
        with _native_lock(entry):
            if entry.exists():
                return
            capture = TurnCapture.begin(
                cwd=cwd,
                session_id=state.session_id,
                turn_id=turn_id,
                harness="codex",
                task_input={
                    "messages": [{"role": "user", "content": payload.get("prompt")}],
                    "cwd": cwd,
                    "model": payload.get("model"),
                    "reasoning_effort": payload.get("reasoning_effort"),
                    "instruction_source": "native rollout session_meta and turn_context",
                    "lifecycle": "native UserPromptSubmit",
                },
            )
            if capture is None:
                return
            capture.bind(
                thread_id=state.thread_id, turn_id=turn_id, native_home=Path(state.codex_home)
            )
            capture.manifest["native_home"] = state.codex_home
            capture.persist()
            entry.write_text(capture.directory.name, encoding="utf-8")
    except Exception:  # noqa: BLE001 - capture cannot change a policy verdict or task
        return


def finish_native(session_id: str, method: str, params: dict[str, Any]) -> str | None:
    if not capture_enabled() or params.get("willRetry") is True:
        return None
    try:
        thread_id = params.get("threadId")
        turn = params.get("turn", {})
        turn_id = turn.get("id") or params.get("turnId")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            return None
        root = capture_root()
        entry = root / ".native-index" / _key(session_id, thread_id, turn_id)
        if not entry.is_file():
            return None
        with _native_lock(entry):
            capture_id = entry.read_text(encoding="utf-8")
            import uuid

            uuid.UUID(capture_id)
            directory = root / capture_id
            manifest = json.loads((directory / "manifest.json").read_text())
            if manifest["completed_at"] is not None:
                return capture_id
            capture = TurnCapture(directory, manifest)
            capture.native_home = Path(manifest["native_home"])
            state = "completed"
            if turn.get("status") == "interrupted":
                state = "cancelled"
            elif method == "turn/failed" or turn.get("status") == "failed":
                state = "failed"
            capture.finish(
                state, {"native_status": turn.get("status"), "error": turn.get("error")}
            )
            return capture_id
    except Exception:  # noqa: BLE001 - capture cannot fail forwarding
        return None


def close_native(bridge_dir: Path) -> None:
    """Finalize incomplete captures only after the owned app-server is stopped."""
    if not capture_enabled():
        return
    try:
        state = read_bridge_state(bridge_dir)
        if state is None:
            return
        for path in capture_root().glob("*/manifest.json"):
            manifest = json.loads(path.read_text())
            if (
                manifest.get("native_home") == state.codex_home
                and manifest.get("completed_at") is None
            ):
                finish_native(
                    state.session_id,
                    "turn/failed",
                    {
                        "threadId": manifest["codex_thread_id"],
                        "turn": {
                            "id": manifest["codex_turn_id"],
                            "status": "failed",
                            "error": "app_server_closed_without_terminal_event",
                        },
                    },
                )
    except Exception:  # noqa: BLE001 - best effort shutdown evidence
        return


@contextmanager
def _native_lock(entry: Path) -> Iterator[None]:
    import fcntl

    with entry.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
