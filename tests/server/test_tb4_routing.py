import json

import pytest
from pydantic import ValidationError

from omnigent.server.o3_routing_review.session_policy import protect_recorded_policy
from omnigent.server.schemas import SessionCreateRequest
from omnigent.server.tb4_routing.service import TB4Policy, bind_policy, evaluate


def fixtures():
    registry = {
        "models": [
            {
                "model": "model-a",
                "score": 40,
                "effort": "high",
                "configuration": "high",
                "name": "Model A",
                "source_url": "https://example.com/a",
            }
        ]
    }
    catalogue = [
        {
            "id": "provider/model-a",
            "owned_by": "provider",
            "capabilities": {"tool_calling": True, "effort_tiers": ["high"]},
            "context_length": 128000,
            "pricing": {"input": 1, "output": 4},
        }
    ]
    evidence = {"providers": {"provider": {"active": True, "cost_class": "free"}}}
    return catalogue, evidence, registry


def test_floor_unknown_models_and_opaque_aliases():
    rows, evidence, registry = fixtures()
    assert evaluate(TB4Policy(minimum_score=40), rows, evidence, registry)["eligible_routes"] == 1
    assert (
        evaluate(TB4Policy(minimum_score=40.1), rows, evidence, registry)["eligible_routes"] == 0
    )
    rows[0]["id"] = "provider/big-pickle"
    assert evaluate(TB4Policy(minimum_score=0), rows, evidence, registry)["eligible_routes"] == 0


def test_cost_does_not_assume_unknown_is_free_and_rates_are_both_bounded():
    rows, evidence, registry = fixtures()
    evidence["providers"]["provider"].pop("cost_class")
    assert evaluate(TB4Policy(), rows, evidence, registry)["eligible_routes"] == 0
    assert (
        evaluate(
            TB4Policy(
                cost_mode="metered", max_input_usd_per_million=1, max_output_usd_per_million=4
            ),
            rows,
            evidence,
            registry,
        )["eligible_routes"]
        == 1
    )
    assert (
        evaluate(
            TB4Policy(cost_mode="metered", max_output_usd_per_million=3), rows, evidence, registry
        )["eligible_routes"]
        == 0
    )
    rows[0]["pricing"]["input"] = None
    assert (
        evaluate(TB4Policy(cost_mode="metered"), rows, evidence, registry)["eligible_routes"] == 0
    )


def test_capabilities_effort_and_unhealthy_route_fail_closed():
    rows, evidence, registry = fixtures()
    for change in [
        {"context_length": 1000},
        {"capabilities": {"tool_calling": False}},
        {"capabilities": {"tool_calling": True, "effort_tiers": ["low"]}},
    ]:
        assert (
            evaluate(TB4Policy(), [{**rows[0], **change}], evidence, registry)["eligible_routes"]
            == 0
        )
    evidence["routes"] = {"provider/model-a": {"status": 401}}
    assert evaluate(TB4Policy(), rows, evidence, registry)["eligible_routes"] == 0
    with pytest.raises(ValidationError):
        TB4Policy(minimum_score=float("nan"))


@pytest.mark.asyncio
async def test_launch_uses_server_decision_and_retains_audit(monkeypatch, tmp_path):
    from omnigent.server.tb4_routing import service

    rows, evidence, registry = fixtures()

    async def preview(policy):
        return evaluate(policy, rows, evidence, registry)

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
    with pytest.raises(Exception, match="No OmniRoute model"):
        await bind_policy(body, "codex-native-ui")


def test_unknown_effort_and_fallback_benchmarks_do_not_qualify():
    rows, evidence, registry = fixtures()
    rows[0]["capabilities"].pop("effort_tiers")
    assert evaluate(TB4Policy(), rows, evidence, registry)["eligible_routes"] == 0
    rows[0]["capabilities"]["effort_tiers"] = ["high"]
    registry["models"][0]["configuration"] = "High, Default Fallback"
    assert evaluate(TB4Policy(), rows, evidence, registry)["eligible_routes"] == 0


def test_account_evidence_expires_and_registry_has_no_fallback(monkeypatch, tmp_path):
    import time

    from omnigent.server.tb4_routing.service import benchmarks, readiness

    path = tmp_path / "evidence.json"
    monkeypatch.setenv("OMNIGENT_TB4_ROUTE_EVIDENCE", str(path))
    path.write_text(json.dumps({"observed_at": time.time() - 1201, "providers": {"a": {}}}))
    assert readiness() == {}
    path.write_text(json.dumps({"observed_at": time.time(), "providers": {"a": {}}}))
    assert "a" in readiness()["providers"]
    registry = benchmarks()
    assert len(registry["models"]) > 100
    assert all("fallback" not in r["configuration"].lower() for r in registry["models"])


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
        rows, evidence, registry = fixtures()
        return evaluate(policy, rows, evidence, registry)

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
