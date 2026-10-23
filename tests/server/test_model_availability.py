from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.tb4_routing.routes import create_tb4_router


@pytest.mark.asyncio
async def test_shared_gateway_delegation_and_protocol_failure(monkeypatch):
    from unittest.mock import AsyncMock

    from omnigent.server.tb4_routing import availability, service

    result = {"authority": "omniroute", "protocol_version": 1, "run": {"id": "central"}}
    request = AsyncMock(return_value=SimpleNamespace(body=result))
    monkeypatch.setattr(
        availability.OmniRouteClient, "from_env", lambda: SimpleNamespace(_request=request)
    )
    assert (await availability.gateway_request("GET"))["run"]["id"] == "central"
    await availability.gateway_request("POST", "/test", {"cost_mode": "included"})
    request.assert_awaited_with(
        "POST", "/api/model-availability/test", body={"cost_mode": "included"}, timeout=30
    )
    for invalid in (
        [],
        {"authority": "other", "protocol_version": 1},
        {"authority": "omniroute", "protocol_version": 2},
    ):
        request.return_value = SimpleNamespace(body=invalid)
        with pytest.raises(OmnigentError, match="no local fallback"):
            await availability.gateway_request("GET")
    request.side_effect = availability.OmniRouteError("private upstream failure")
    with pytest.raises(OmnigentError, match="no local fallback") as error:
        await availability.gateway_request("POST", "/test", {})
    assert "private" not in str(error.value)
    request.side_effect = None
    monkeypatch.setenv("OMNIGENT_TB4_ROUTING_ENABLED", "1")
    policy = service.TB4Policy(minimum_score=40)
    result = {
        "authority": "omniroute",
        "protocol_version": 1,
        "eligible": [],
        "policy": policy.model_dump(),
    }
    request.return_value = SimpleNamespace(body=result)
    assert await service.catalogue(policy) == result
    result["policy"]["minimum_score"] = 0
    with pytest.raises(OmnigentError, match="Invalid OmniRoute selection"):
        await service.catalogue(policy)


@pytest.mark.asyncio
async def test_availability_auth_admin_csrf_and_feature_gate(monkeypatch):
    from unittest.mock import AsyncMock

    from omnigent.server.tb4_routing import routes

    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("OMNIGENT_TB4_ROUTING_ENABLED", "1")
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handler(request, error):
        return JSONResponse(status_code=error.http_status, content={"error": error.message})

    permissions = SimpleNamespace(is_admin=lambda user: user == "admin@example.com")
    app.include_router(
        create_tb4_router(
            UnifiedAuthProvider(source="header", local_single_user=False), permissions
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/routing/availability")).status_code == 401
        assert (
            await client.get(
                "/v1/routing/availability", headers={"X-Forwarded-Email": "user@example.com"}
            )
        ).status_code == 403
        auth = {"X-Forwarded-Email": "admin@example.com"}
        assert (
            await client.post(
                "/v1/routing/availability/test",
                json={},
                headers={**auth, "Origin": "https://evil.example"},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/v1/routing/availability/test",
                content="{}",
                headers={**auth, "Content-Type": "text/plain"},
            )
        ).status_code == 415
        delegated = AsyncMock(return_value={"targets": 41})
        monkeypatch.setattr(routes, "gateway_request", delegated)
        for check_tools in (False, True):
            response = await client.post(
                "/v1/routing/availability/plan",
                json={"cost_mode": "free", "check_tools": check_tools, "timeout_seconds": 180},
                headers=auth,
            )
            assert response.status_code == 200
            body = delegated.call_args.args[2]
            assert body["check_tools"] is check_tools
            assert body["timeout_seconds"] == 180
            assert body["cost_mode"] == "free"
        response = await client.post("/v1/routing/availability/plan", json={}, headers=auth)
        assert response.status_code == 200
        assert delegated.call_args.args[2]["check_tools"] is False
        assert delegated.call_args.args[2]["timeout_seconds"] == 60
        assert (
            await client.post(
                "/v1/routing/availability/plan", json={"timeout_seconds": 181}, headers=auth
            )
        ).status_code == 422
        monkeypatch.setenv("OMNIGENT_TB4_ROUTING_ENABLED", "0")
        assert (
            await client.post("/v1/routing/availability/test", json={}, headers=auth)
        ).status_code == 404
