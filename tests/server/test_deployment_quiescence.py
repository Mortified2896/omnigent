"""Admission and server-wide quiescence behavior for single-service activation."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from omnigent.deployment_quiescence import ComponentObservation
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as app_module
from omnigent.server.deployment_quiescence import (
    DeploymentQuiescence,
    QuiescenceBlocked,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


def _new_manager() -> DeploymentQuiescence:
    manager = DeploymentQuiescence(required_components=("remote_peer_inventory",))
    manager.register_component(
        "remote_peer_inventory",
        lambda: ComponentObservation("remote_peer_inventory", "no-remote-peers", 0),
    )
    return manager


def _certificate(manager: DeploymentQuiescence):
    return manager.issue_certificate(
        state_identity="state-identity",
        state_generation="state-generation",
        persistent_state_digest="state-digest",
    )


def test_admission_and_fence_race_never_leaves_a_valid_certificate() -> None:
    """Every racing writer is either counted or rejected with invalidation."""
    for _ in range(40):
        manager = _new_manager()
        barrier = Barrier(3)
        release = Event()
        outcome_ready = Event()
        admitted: list[bool] = []

        def writer(
            barrier: Barrier = barrier,
            manager: DeploymentQuiescence = manager,
            admitted: list[bool] = admitted,
            release: Event = release,
            outcome_ready: Event = outcome_ready,
        ) -> None:
            barrier.wait()
            lease = manager.try_admit("racing_mutator")
            admitted.append(lease is not None)
            outcome_ready.set()
            if lease is not None:
                assert release.wait(timeout=2)
                lease.release()

        def fence(
            barrier: Barrier = barrier,
            manager: DeploymentQuiescence = manager,
        ) -> None:
            barrier.wait()
            manager.fence()

        writer_thread = Thread(target=writer)
        fence_thread = Thread(target=fence)
        writer_thread.start()
        fence_thread.start()
        barrier.wait()
        fence_thread.join(timeout=2)
        assert not fence_thread.is_alive()
        assert outcome_ready.wait(timeout=2)

        if admitted == [True]:
            with pytest.raises(QuiescenceBlocked):
                _certificate(manager)
            release.set()
            writer_thread.join(timeout=2)
            assert not writer_thread.is_alive()
            _certificate(manager)
        else:
            assert admitted == [False]
            cert = _certificate(manager)
            assert manager.try_admit("late_mutator") is None
            with pytest.raises(QuiescenceBlocked, match="certificate_unknown_or_invalidated"):
                manager.verify_certificate(
                    cert.certificate_id,
                    state_identity="state-identity",
                    state_generation="state-generation",
                )


def test_unknown_or_disconnected_remote_writer_blocks_certificate() -> None:
    manager = _new_manager()
    assert manager.note_external_writer("runner", "runner-17") is True
    assert manager.fence() == 1
    with pytest.raises(QuiescenceBlocked, match="remote_writers_unknown"):
        _certificate(manager)


def test_process_generation_and_new_fence_work_invalidate_certificate() -> None:
    first = _new_manager()
    first.fence()
    cert = _certificate(first)
    second = _new_manager()
    assert cert.process_generation != second.process_generation
    with pytest.raises(QuiescenceBlocked, match="certificate_unknown_or_invalidated"):
        second.verify_certificate(
            cert.certificate_id,
            state_identity="state-identity",
            state_generation="state-generation",
        )

    assert first.try_admit("late_write") is None
    with pytest.raises(QuiescenceBlocked, match="certificate_unknown_or_invalidated"):
        first.verify_certificate(
            cert.certificate_id,
            state_identity="state-identity",
            state_generation="state-generation",
        )


async def _control(path: Path, operation: str, **fields: Any) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(json.dumps({"operation": operation, **fields}).encode() + b"\n")
    await writer.drain()
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=3)
        assert line.endswith(b"\n")
        return json.loads(line)
    finally:
        writer.close()
        await writer.wait_closed()


async def test_real_omnigent_server_http_and_websocket_fence_race(
    runtime_init: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network clients prove drain and the blocked-WS-receive admission race."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    database = state_root / "omnigent.db"
    db_uri = f"sqlite:///{database}"
    conversation_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(state_root / "artifacts"))
    slow_request_started = asyncio.Event()
    finish_slow_request = asyncio.Event()
    websocket_waiting_for_message = asyncio.Event()
    websocket_messages: list[str] = []

    router = APIRouter()

    @router.post("/quiescence/slow-write")
    async def slow_write() -> dict[str, str]:
        slow_request_started.set()
        await finish_slow_request.wait()
        conversation = conversation_store.create_conversation(title="admitted before fence")
        return {"conversation_id": conversation.id}

    @router.websocket("/quiescence/write-socket")
    async def write_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        websocket_waiting_for_message.set()
        try:
            message = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        websocket_messages.append(message)
        conversation_store.create_conversation(title="websocket write reached app")
        await websocket.send_text("handled")

    monkeypatch.setenv("OMNIGENT_DEPLOYMENT_CONTROL_SOCKET", str(tmp_path / "q.sock"))
    monkeypatch.setenv("OMNIGENT_DEPLOYMENT_STATE_ROOT", str(state_root))
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", tmp_path / "missing-web-ui")
    app = app_module.create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        extra_routers=[(router, "/v1", ["quiescence-test"])],
    )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="on",
            ws="websockets",
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    ws = None
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if server_task.done():
                    server_task.result()
                await asyncio.sleep(0.02)
        control_path = tmp_path / "q.sock"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            # Controller-managed candidates start behind the fence. Health
            # probes can still read, while writes wait for the controller to
            # explicitly open this exact process-local generation.
            startup = await _control(control_path, "status")
            assert startup["ok"] is True
            assert startup["fenced"] is True
            before_open = await client.post("/v1/quiescence/slow-write")
            assert before_open.status_code == 423
            assert not slow_request_started.is_set()
            opened = await _control(
                control_path,
                "open_writes",
                fence_generation=startup["fence_generation"],
            )
            assert opened["ok"] is True
            assert opened["fenced"] is False

            write_task = asyncio.create_task(client.post("/v1/quiescence/slow-write"))
            await asyncio.wait_for(slow_request_started.wait(), timeout=3)
            ws = await connect(f"ws://127.0.0.1:{port}/v1/quiescence/write-socket")
            await asyncio.wait_for(websocket_waiting_for_message.wait(), timeout=3)

            fence = await _control(control_path, "fence")
            assert fence["ok"] is True
            blocked = await _control(control_path, "observe")
            assert blocked["ok"] is False
            assert "admitted_work_active:1" in blocked["blockers"]

            rejected = await client.post("/v1/quiescence/slow-write")
            assert rejected.status_code == 423

            await ws.send("must not reach write handling")
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=3)
            assert websocket_messages == []

            finish_slow_request.set()
            result = await asyncio.wait_for(write_task, timeout=5)
            assert result.status_code == 200
            conversation_id = result.json()["conversation_id"]
            observed = await _control(control_path, "observe")
            assert observed["ok"] is True, observed
            certificate = observed["certificate"]
            assert certificate["fence_generation"] == fence["fence_generation"]
            assert certificate["process_generation"] == fence["process_generation"]
            assert all(item["active_work"] == 0 for item in certificate["components"])
            verified = await _control(
                control_path,
                "verify",
                certificate_id=certificate["certificate_id"],
            )
            assert verified["ok"] is True

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    ("out-of-band state drift", bytes.fromhex(conversation_id)),
                )
            stale_state = await _control(
                control_path,
                "verify",
                certificate_id=certificate["certificate_id"],
            )
            assert stale_state["ok"] is False
            assert "state_generation_changed" in stale_state["blockers"]

            refreshed = await _control(control_path, "observe")
            assert refreshed["ok"] is True
            second_certificate = refreshed["certificate"]
            assert (
                await _control(
                    control_path,
                    "verify",
                    certificate_id=second_certificate["certificate_id"],
                )
            )["ok"] is True

            with pytest.raises(QuiescenceBlocked, match="database_write_fenced"):
                conversation_store.create_conversation(title="unadmitted background write")

            later_rejected = await client.post("/v1/quiescence/slow-write")
            assert later_rejected.status_code == 423
            stale = await _control(
                control_path,
                "verify",
                certificate_id=second_certificate["certificate_id"],
            )
            assert stale["ok"] is False

            with conversation_store._conv_engine.connect() as connection:
                row = connection.execute(
                    text("SELECT title FROM conversations WHERE id = :id"),
                    {"id": bytes.fromhex(conversation_id)},
                ).scalar_one()
            assert row == "out-of-band state drift"
            opened = await _control(
                control_path,
                "open_writes",
                fence_generation=fence["fence_generation"],
            )
            assert opened["ok"] is True
            resumed = await client.post("/v1/quiescence/slow-write")
            assert resumed.status_code == 200
    finally:
        finish_slow_request.set()
        if ws is not None:
            await ws.close()
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=15)
        finally:
            listener.close()
