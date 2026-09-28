"""Authorization, CSRF, and write-fence coverage for deployment actions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as app_module
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.deployment_controller import DeploymentControllerUnavailable
from omnigent.server.deployment_fence import DeploymentWriteFenceMiddleware
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio

_ADMIN = "admin@example.com"
_READER = "reader@example.com"


class FakeController:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.plan = {
            "object": "deployment",
            "status": "ready",
            "blockers": [],
            "can_sync": True,
            "plan_id": "plan-test",
            "expires_at": 2_000.0,
            "source": {"custom_version": "git-aaaaaaaaaaaa"},
            "target": {"custom_version": "git-bbbbbbbbbbbb"},
        }
        self.job = {
            "id": "job-test",
            "status": "queued",
            "requested_by": _ADMIN,
            "reason": None,
        }
        self.unavailable = False

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        if self.unavailable:
            raise DeploymentControllerUnavailable("not installed")
        if payload["operation"] == "plan":
            return {"ok": True, "plan": self.plan}
        if payload["operation"] == "enqueue":
            return {"ok": True, "job": self.job}
        if payload["operation"] == "job":
            return {"ok": True, "job": self.job}
        raise AssertionError(payload)


@pytest.fixture()
def deployment_setup(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, FakeController]:
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", tmp_path / "missing-web-ui")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user(_ADMIN, is_admin=True)
    permission_store.ensure_user(_READER)
    controller = FakeController()
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        deployment_controller=controller,
    )
    return app, controller


@pytest_asyncio.fixture()
async def deployment_client(
    deployment_setup: tuple[FastAPI, FakeController],
) -> AsyncIterator[httpx.AsyncClient]:
    app, _controller = deployment_setup
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_deployment_requires_authentication_and_admin(
    deployment_client: httpx.AsyncClient,
) -> None:
    unauthenticated = await deployment_client.get("/v1/deployment")
    non_admin = await deployment_client.get(
        "/v1/deployment", headers={"X-Forwarded-Email": _READER}
    )

    assert unauthenticated.status_code == 401
    assert non_admin.status_code == 403


async def test_admin_can_read_plan_but_browser_cannot_submit_evidence(
    deployment_client: httpx.AsyncClient,
    deployment_setup: tuple[FastAPI, FakeController],
) -> None:
    _app, controller = deployment_setup
    headers = {"X-Forwarded-Email": _ADMIN}
    plan_response = await deployment_client.get("/v1/deployment", headers=headers)
    assert plan_response.status_code == 200
    assert plan_response.json()["plan_id"] == "plan-test"
    assert controller.calls == [{"operation": "plan"}]

    response = await deployment_client.post(
        "/v1/deployment/sync-o2",
        json={
            "plan_id": "plan-test",
            "idempotency_key": "123e4567-e89b-42d3-a456-426614174000",
        },
        headers={**headers, "Origin": "http://127.0.0.1"},
    )
    assert response.status_code == 202
    assert response.json()["job"]["id"] == "job-test"
    assert controller.calls[-1] == {
        "operation": "enqueue",
        "plan_id": "plan-test",
        "idempotency_key": "123e4567-e89b-42d3-a456-426614174000",
        "requested_by": _ADMIN,
    }


async def test_sync_requires_json_and_trusted_origin(
    deployment_client: httpx.AsyncClient,
) -> None:
    headers = {"X-Forwarded-Email": _ADMIN}
    missing_json = await deployment_client.post(
        "/v1/deployment/sync-o2",
        content='{"plan_id":"plan-test","idempotency_key":"123e4567-e89b-42d3-a456-426614174001"}',
        headers={**headers, "Content-Type": "text/plain"},
    )
    untrusted_origin = await deployment_client.post(
        "/v1/deployment/sync-o2",
        json={
            "plan_id": "plan-test",
            "idempotency_key": "123e4567-e89b-42d3-a456-426614174001",
        },
        headers={**headers, "Origin": "https://evil.example"},
    )

    assert missing_json.status_code == 415
    assert untrusted_origin.status_code == 403


async def test_sync_rejects_extra_browser_fields(
    deployment_client: httpx.AsyncClient,
) -> None:
    response = await deployment_client.post(
        "/v1/deployment/sync-o2",
        json={
            "plan_id": "plan-test",
            "idempotency_key": "123e4567-e89b-42d3-a456-426614174002",
            "source_sha": "a" * 40,
            "health": True,
            "force": True,
        },
        headers={"X-Forwarded-Email": _ADMIN, "Origin": "http://127.0.0.1"},
    )

    assert response.status_code == 422


async def test_uninstalled_controller_disables_plan_safely(
    deployment_client: httpx.AsyncClient,
    deployment_setup: tuple[FastAPI, FakeController],
) -> None:
    _app, controller = deployment_setup
    controller.unavailable = True
    response = await deployment_client.get(
        "/v1/deployment", headers={"X-Forwarded-Email": _ADMIN}
    )

    assert response.status_code == 200
    assert response.json() == {
        "object": "deployment",
        "status": "blocked",
        "blockers": ["controller_unavailable"],
        "can_sync": False,
        "source": None,
        "target": None,
        "expires_at": None,
        "plan_id": None,
    }


async def test_write_fence_blocks_every_mutating_http_method_but_allows_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(state_dir))
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/health")
    async def accidental_health_write() -> dict[str, str]:
        return {"status": "written"}

    @app.post("/v1/write")
    async def write() -> dict[str, str]:
        return {"status": "written"}

    fenced = DeploymentWriteFenceMiddleware(app)
    state_dir.joinpath("deployment-write-fence").write_text("{\"transaction\":\"t\"}\n")
    transport = httpx.ASGITransport(app=fenced)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health_response = await client.get("/health")
        health_write_response = await client.post("/health")
        write_response = await client.post("/v1/write")

    assert health_response.status_code == 200
    assert health_write_response.status_code == 423
    assert write_response.status_code == 423
    assert write_response.json()["error"]["code"] == "deployment_write_fenced"


async def test_write_fence_closes_existing_websocket_on_next_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(state_dir))
    app_started = asyncio.Event()
    allow_receive = asyncio.Event()
    received: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    async def websocket_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "websocket.accept"})
        app_started.set()
        await allow_receive.wait()
        received.append(await receive())

    middleware = DeploymentWriteFenceMiddleware(cast(Any, websocket_app))

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.receive", "text": "late"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    task = asyncio.create_task(
        middleware(
            {"type": "websocket", "path": "/v1/runner", "headers": []},
            receive,
            cast(Any, send),
        )
    )
    await app_started.wait()
    state_dir.joinpath("deployment-write-fence").write_text('{"transaction":"t"}\n')
    allow_receive.set()
    await task

    assert received == [{"type": "websocket.disconnect", "code": 1013}]
    assert any(message.get("type") == "websocket.close" for message in sent)
