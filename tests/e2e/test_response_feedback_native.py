"""Opt-in real native answer feedback acceptance on a disposable server."""

import json
import os
import shutil
import signal
import subprocess

import pytest

from tests.e2e.test_host_codex_native_e2e import (
    _codex_native_agent_id,
    _online_host_id,
    _ordered_message_items,
    _poll_for_assistant_marker,
    _spawn_host_daemon,
)


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason="Requires opted-in authenticated Codex CLI",
)
def test_native_answer_feedback(live_server, http_client, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "host-data"))
    monkeypatch.setenv("OMNIGENT_LOG_TO_STDERR", "1")
    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host = _online_host_id(http_client, timeout=120)
        created = http_client.post(
            "/v1/sessions",
            json={
                "agent_id": _codex_native_agent_id(http_client),
                "host_id": host,
                "workspace": str(workspace),
            },
            timeout=60,
        )
        created.raise_for_status()
        session = created.json()["id"]
        sent = http_client.post(
            f"/v1/sessions/{session}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Reply exactly: Feedback acceptance confirms "
                                "durable response identity. "
                                "Do not use tools."
                            ),
                        }
                    ],
                },
            },
            timeout=60,
        )
        sent.raise_for_status()
        _poll_for_assistant_marker(
            http_client, session_id=session, marker="durable response identity", timeout=180
        )
        messages = _ordered_message_items(http_client, session_id=session)
        answer = next(item for item in messages if item.get("role") == "assistant")
        response = answer["response_id"]
        url = f"/v1/sessions/{session}/response-feedback"
        saved = http_client.put(
            f"{url}/{response}", json={"rating": 1, "comment": "Clear and concise."}
        )
        saved.raise_for_status()
        assert http_client.get(url).json() == [saved.json()]
        evidence_dir = tmp_path
        (evidence_dir / "native-feedback.json").write_text(
            json.dumps(
                {
                    "session_id": session,
                    "response_id": response,
                    "feedback": saved.json(),
                    "answer": answer,
                },
                indent=2,
            )
        )
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
