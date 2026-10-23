import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.tb4_routing import availability as a
from omnigent.server.tb4_routing.availability_store import ProbeStore
from omnigent.server.tb4_routing.routes import create_tb4_router
from omnigent.server.tb4_routing.service import TB4Policy, evaluate
from tests.server.test_tb4_routing import fixtures


def view():
    rows, evidence, registry = fixtures()
    connections = [{"id": "one", "provider": "provider", "isActive": True}]
    return a.inventory(rows, connections, evidence, registry, {})


def test_inventory_keeps_unknown_scores_exact_routes_and_safe_metadata():
    rows, evidence, registry = fixtures()
    rows += [{**rows[0], "id": "provider/unknown"}, {**rows[0], "id": "alias/model-a"}, rows[0]]
    connections = [
        {
            "id": "one",
            "provider": "provider",
            "isActive": True,
            "apiKey": "secret",
            "email": "private",
        }
    ]
    data = a.inventory(rows, connections, evidence, registry, {})
    assert [r["route"] for r in data["rows"]] == ["provider/model-a", "provider/unknown"]
    assert data["rows"][1]["score"] is None
    assert "secret" not in json.dumps(data) and "private" not in json.dumps(data)
    rows[0]["capabilities"]["effort_tiers"] = ["low"]
    assert a.inventory(rows[:1], connections, evidence, registry, {})["rows"][0]["score"] is None


def test_plan_enforces_both_cost_rates_unknown_prices_and_budget():
    free = view()["rows"][0]
    unknown = {
        **free,
        "route": "provider/unknown",
        "cost_class": "unknown",
        "estimated_probe_usd": None,
    }
    paid = {
        **free,
        "route": "provider/paid",
        "cost_class": "metered",
        "input_usd_per_million": 1,
        "output_usd_per_million": 4,
        "estimated_probe_usd": 0.5,
    }
    assert a.plan([free, paid, unknown], a.ProbePolicy())[0] == [free]
    policy = a.ProbePolicy(cost_mode="metered", max_output_usd_per_million=3)
    assert a.plan([paid], policy)[0] == []
    policy = a.ProbePolicy(cost_mode="metered", estimated_budget_usd=0.6)
    targets, skipped, estimate = a.plan([paid, {**paid, "route": "provider/paid2"}], policy)
    assert len(targets) == 1 and estimate == 0.5 and skipped["Estimated batch budget reached"] == 1
    assert not a.plan([{**free, "account_ready": False}], a.ProbePolicy())[0]
    assert not a.plan([{**free, "status": "available"}], a.ProbePolicy())[0]
    assert a.plan([{**free, "status": "available"}], a.ProbePolicy(retest=True))[0]


def test_staleness_and_quota_placeholders_do_not_claim_remaining_use():
    rows, evidence, registry = fixtures()
    result = {
        "route": rows[0]["id"],
        "effort": "high",
        "checked_at": time.time() - 86401,
        "status": "available",
    }
    connections = [{"id": "one", "provider": "provider", "isActive": True}]
    plans = [
        {
            "provider": "provider",
            "dimensions": [{"limit": 2.22e-16, "unit": "tokens", "window": "5h"}],
            "source": "auto",
        }
    ]
    data = a.inventory(
        rows, connections, evidence, registry, {(result["route"], "high"): result}, {}, plans
    )
    assert data["rows"][0]["status"] == "stale"
    assert data["providers"][0]["policy_limits"] == []
    connections[0]["providerSpecificData"] = {"apiKeyHealth": {"primary": {"status": "invalid"}}}
    assert not a.inventory(rows, connections, evidence, registry, {})["rows"][0]["account_ready"]


@pytest.mark.asyncio
async def test_probe_requires_exact_model_tool_and_continuation():
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload["model"] == "provider/model-a"
        assert payload["max_output_tokens"] == 256 and payload["store"] is False
        assert request.headers["X-OmniRoute-No-Cache"] == "true"
        if len(seen) == 1:
            token = payload["input"][0]["content"].split("token ")[1].split(".")[0]
            return httpx.Response(
                200,
                json={
                    "model": "model-a",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call",
                            "name": "availability_echo",
                            "arguments": json.dumps({"token": token}),
                        }
                    ],
                },
            )
        assert payload["input"][-1]["type"] == "function_call_output"
        return httpx.Response(
            200, json={"model": "model-a", "output_text": payload["input"][-1]["output"]}
        )

    async with httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.MockTransport(handler)
    ) as client:
        result = await a.probe(client, view()["rows"][0], "test")
    assert result["status"] == "available" and result["tools_ok"] and len(seen) == 2
    assert "arguments" not in result and "output_text" not in result


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {}, "failed"),
        (429, {}, "limited"),
        (404, {}, "failed"),
        (200, {"model": "model-a", "output": []}, "incompatible"),
        (200, {"model": "another-model", "output_text": "OK"}, "incompatible"),
        (200, {"model": "model-a", "status": "incomplete"}, "incompatible"),
    ],
)
@pytest.mark.asyncio
async def test_failed_probes_never_fallback(status, body, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.MockTransport(handler)
    ) as client:
        result = await a.probe(client, view()["rows"][0], "test")
    assert result["status"] == expected and len(requests) == 1


def test_probe_receipts_admit_cold_routes_and_reject_tool_failure():
    rows, evidence, registry = fixtures()
    evidence["routes"] = {}
    p = {
        "route": rows[0]["id"],
        "effort": "high",
        "checked_at": time.time(),
        "status": "available",
    }
    evidence["probes"] = [p]
    assert evaluate(TB4Policy(), rows, evidence, registry)["eligible_routes"] == 1
    p["status"] = "incompatible"
    evidence["routes"] = {
        rows[0]["id"]: {"status": 200, "observed_at": time.time(), "endpoint": "/v1/responses"}
    }
    assert evaluate(TB4Policy(), rows, evidence, registry)["eligible_routes"] == 0
    p.update(status="available", effort="low")
    evidence["routes"] = {}
    assert evaluate(TB4Policy(), rows, evidence, registry)["eligible_routes"] == 0


@pytest.mark.asyncio
async def test_single_batch_cancel_and_restart_recovery(monkeypatch, tmp_path):
    store = ProbeStore(tmp_path)
    service = a.AvailabilityService(store)
    client = SimpleNamespace(base_url="http://gateway", _authorization="Bearer test")
    monkeypatch.setattr(a.OmniRouteClient, "from_env", lambda: client)

    async def overview(refresh=False):
        return view()

    monkeypatch.setattr(service, "overview", overview)
    entered = asyncio.Event()

    async def probing(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(a, "probe", probing)
    monkeypatch.setattr(a, "readiness", lambda: {"providers": {"provider": {"active": True}}})
    await service.start(a.ProbePolicy(), "admin")
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(OmnigentError, match="already running"):
        await service.start(a.ProbePolicy(), "admin")
    await service.cancel()
    assert store.latest()["status"] == "cancelled"
    run = store.latest()
    run["status"] = "running"
    store.run(run)
    restarted = a.AvailabilityService(ProbeStore(tmp_path))
    restarted._cache = (time.time(), [], [])
    monkeypatch.setenv("OMNIGENT_TB4_ROUTING_ENABLED", "1")
    assert (await restarted.overview())["run"]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_availability_auth_admin_csrf_and_feature_gate(monkeypatch):
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
        monkeypatch.setenv("OMNIGENT_TB4_ROUTING_ENABLED", "0")
        assert (
            await client.post("/v1/routing/availability/test", json={}, headers=auth)
        ).status_code == 404
