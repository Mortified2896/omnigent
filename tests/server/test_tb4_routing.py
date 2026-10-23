import json

import pytest

from omnigent.server.o3_routing_review.session_policy import protect_recorded_policy
from omnigent.server.schemas import SessionCreateRequest
from omnigent.server.tb4_routing.service import TB4Policy, bind_policy


def decision(policy):
    return {
        "authority": "omniroute",
        "protocol_version": 1,
        "policy": policy.model_dump(),
        "eligible_routes": 1,
        "eligible_models": 1,
        "eligible": [
            {"route": "provider/model-a", "effort": "high", "score": 40, "cost_class": "free"}
        ],
        "benchmark": {"retrieved_at": "2026-10-22"},
    }


@pytest.mark.asyncio
async def test_launch_uses_server_decision_and_retains_audit(monkeypatch, tmp_path):
    from omnigent.server.tb4_routing import service

    async def preview(policy):
        return decision(policy)

    monkeypatch.setattr(service, "catalogue", preview)
    monkeypatch.setenv("OMNIGENT_TB4_AUDIT_DIR", str(tmp_path))
    body = SessionCreateRequest(
        agent_id="00000000-0000-0000-0000-000000000001", tb4_policy=TB4Policy()
    )
    bound = await bind_policy(body, "codex-native-ui")
    assert bound.model_override == "provider/model-a"
    assert bound.reasoning_effort == "high"
    assert bound.cost_control_mode_override == "off"
    assert bound.subagent_routing_override == "off"
    assert bound.labels["omnigent.access_lane"] == "omniroute"
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["selected"]["score"] >= body.tb4_policy.minimum_score
    with pytest.raises(Exception, match="owns the model"):
        await bind_policy(body.model_copy(update={"model_override": "bypass"}), "codex-native-ui")
    with pytest.raises(Exception, match="server-owned"):
        await bind_policy(
            body.model_copy(update={"labels": {"omnigent.tb4.receipt": "forged"}}),
            "codex-native-ui",
        )
    with pytest.raises(Exception, match="approval-bound"):
        protect_recorded_policy(
            bound.labels,
            {"model_override": bound.model_override},
            {"model_override": "paid-fallback"},
        )


@pytest.mark.asyncio
async def test_empty_pool_never_starts_fallback(monkeypatch):
    from omnigent.server.tb4_routing import service

    async def preview(policy):
        return {"eligible": []}

    monkeypatch.setattr(service, "catalogue", preview)
    body = SessionCreateRequest(
        agent_id="00000000-0000-0000-0000-000000000001", tb4_policy=TB4Policy()
    )
    with pytest.raises(Exception, match="No verified OmniRoute route"):
        await bind_policy(body, "codex-native-ui")


@pytest.mark.asyncio
async def test_preview_auth_origin_and_json_guards(monkeypatch):
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from omnigent.errors import OmnigentError
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.tb4_routing import routes

    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")

    async def preview(policy):
        return decision(policy)

    monkeypatch.setattr(routes, "catalogue", preview)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(_request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    app.include_router(
        routes.create_tb4_router(UnifiedAuthProvider(source="header", local_single_user=False)),
        prefix="/v1",
    )
    auth = {"X-Forwarded-Email": "owner@example.com"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/routing/tb4")).status_code == 401
        assert (await client.post("/v1/routing/tb4/preview", json={})).status_code == 401
        assert (
            await client.post(
                "/v1/routing/tb4/preview",
                json={},
                headers={**auth, "Origin": "https://evil.example"},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/v1/routing/tb4/preview",
                content="{}",
                headers={**auth, "Content-Type": "text/plain"},
            )
        ).status_code == 415
        result = await client.post("/v1/routing/tb4/preview", json={}, headers=auth)
        assert result.status_code == 200 and result.json()["eligible_routes"] == 1


@pytest.mark.asyncio
async def test_launch_revalidates_exact_route_and_endpoint(monkeypatch):
    from omnigent.server.tb4_routing import service

    async def preview(policy):
        return decision(policy)

    monkeypatch.setattr(service, "catalogue", preview)
    monkeypatch.setattr(
        service.OmniRouteClient,
        "from_env",
        lambda: type("Client", (), {"base_url": "http://127.0.0.1:20128"})(),
    )
    result = await service.validate_pinned_launch(
        {}, "provider/model-a", "high", "http://127.0.0.1:20128/v1"
    )
    assert result["route"] == "provider/model-a"
    with pytest.raises(RuntimeError, match="endpoint"):
        await service.validate_pinned_launch({}, "provider/model-a", "high", "https://other/v1")
    with pytest.raises(RuntimeError, match="no fallback"):
        await service.validate_pinned_launch(
            {}, "provider/model-a", "low", "http://127.0.0.1:20128/v1"
        )
