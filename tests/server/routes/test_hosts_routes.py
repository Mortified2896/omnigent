"""Tests for the hosts REST routes (``/v1/hosts``).

The hosts router is only mounted when ``host_store`` is provided to
``create_app``. The standard test ``app`` fixture does not supply one,
so host endpoints return 404. These tests verify the expected behavior
when hosts are not configured, and test the route helpers directly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.skills import request_host_skills


async def test_hosts_not_mounted_without_host_store(client: httpx.AsyncClient) -> None:
    """GET /v1/hosts returns 404 when hosts are not configured."""
    resp = await client.get("/v1/hosts")
    # When host_store is not provided, the router is not mounted at all.
    assert resp.status_code == 404


async def test_get_host_not_mounted(client: httpx.AsyncClient) -> None:
    """GET /v1/hosts/{id} returns 404 when hosts are not configured."""
    resp = await client.get("/v1/hosts/host_nonexistent_12345")
    assert resp.status_code == 404


@pytest.mark.parametrize("outcome,status", [("timeout", 504), ("replaced", 502), ("cancel", None)])
async def test_skills_proxy_cleans_up_unanswered_requests(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int | None
) -> None:
    registry = HostRegistry()
    conn = registry.register(
        host_id="host_skills_test",
        ws=AsyncMock(),
        hello=HostHelloFrame(version="test", frame_protocol_version=1, name="test"),
        owner=None,
    )
    if outcome == "timeout":
        monkeypatch.setattr("omnigent.server.routes.skills._SKILLS_TIMEOUT_S", 0.01)
    elif outcome == "replaced":
        registry.deregister(conn.host_id)
    task = asyncio.create_task(
        request_host_skills(
            host_registry=registry, host_conn=conn, harness="claude-native", path="~"
        )
    )
    if outcome == "cancel":
        await asyncio.wait_for(conn.outbound_queue.get(), timeout=1)
        assert conn.pending_skills
        task.cancel()
    (result,) = await asyncio.gather(task, return_exceptions=True)
    if outcome == "cancel":
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert isinstance(result, HTTPException)
        assert result.status_code == status
    assert conn.pending_skills == {}


def test_codex_subscription_usage_is_host_owner_scoped(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from omnigent.errors import OmnigentError
    from omnigent.server.auth import AuthProvider
    from omnigent.server.routes import hosts

    class Owner(AuthProvider):
        def get_user_id(self, request):
            return request.headers.get("x-test-user")

    class Hosts:
        def get_host(self, host_id):
            return SimpleNamespace(host_id=host_id, user_id="alice")

    class Registry:
        def get(self, host_id):
            return SimpleNamespace(host_id=host_id)

    async def public_limits(**kwargs):
        assert kwargs["harness"] == "codex-account-limits"
        return {"status": "ok", "rate_limits": {"remaining_percent": 72, "windows": []}}

    monkeypatch.setattr(hosts, "_proxy_model_options", public_limits)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request, error):
        return JSONResponse(status_code=error.http_status, content={"detail": error.message})

    app.include_router(
        hosts.create_hosts_router(Registry(), Hosts(), None, auth_provider=Owner()), prefix="/v1"
    )
    with TestClient(app) as client:
        path = "/v1/hosts/host_1/codex-rate-limits"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"x-test-user": "bob"}).status_code == 403
        response = client.get(path, headers={"x-test-user": "alice"})
        assert response.status_code == 200
        assert response.json() == {"remaining_percent": 72, "windows": []}
