"""Snapshot live model metadata and probe only explicitly free-labelled routes."""

import asyncio
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError
from omnigent.server.o3_routing_review.recommendation import load_recommendation_catalogue
from omnigent.server.o3_routing_review.tool_free import PROTOCOL_VERSION, execute
from omnigent.server.o3_routing_review.tool_free_qualification import free_cost_class

ROOT = Path(__file__).parent


async def main():
    outbound = []
    original = httpx.AsyncClient

    async def capture(request):
        if request.url.path == "/v1/responses":
            body = json.loads(request.content)
            outbound.append(
                {
                    k: body[k]
                    for k in ["model", "tools", "tool_choice", "store", "stream", "reasoning"]
                }
            )

    httpx.AsyncClient = lambda **kw: original(event_hooks={"request": [capture]}, **kw)
    client = OmniRouteClient.from_env()
    live = await client.model_ids()
    catalog = (await client._request("GET", "/api/models/catalog")).body["catalog"]
    forecasts = load_recommendation_catalogue(os.environ["OMNIGENT_O3_RECOMMENDATION_CATALOG_DIR"])
    by_id = {row["provider_model_route_id"]: row for row in forecasts.forecasts}
    pricing = (await client._request("GET", "/api/pricing")).body
    rows = []
    for provider, group in catalog.items():
        if provider == "combo":
            continue
        for model in group.get("models", []):
            route = model["id"]
            cost_class = free_cost_class(route, pricing)
            free = cost_class is not None
            forecast = by_id.get(route, {})
            rows.append(
                {
                    "provider": route.split("/", 1)[0],
                    "catalogue_provider": provider,
                    "route": route,
                    "live_present": route in live,
                    "cost_class": cost_class or "not_verified_free",
                    "provider_label": group.get("provider"),
                    "account_requirement": "not exposed by model catalogue",
                    "active": group.get("active"),
                    "context_tokens": model.get("context_length"),
                    "input_modalities": model.get("input_modalities"),
                    "output_modalities": model.get("output_modalities"),
                    "capabilities": model.get("capabilities"),
                    "conservative_quality": forecast.get("capability_score_lower"),
                    "quality_evidence_applicable": forecast.get("adviser_applicable", False),
                    "protocol": PROTOCOL_VERSION,
                    "reasoning_effort": "low",
                    "probe": "not_probed",
                    "exclusion": None
                    if free
                    else "Current zero cost not established; no paid probe",
                }
            )
    sem = asyncio.Semaphore(3)

    async def probe(row):
        if row["cost_class"] not in {"free_label", "zero_priced"} or not row["live_present"]:
            return
        async with sem:
            try:
                async with asyncio.timeout(15):
                    result = await execute(
                        client,
                        route=row["route"],
                        prompt="Reply with OK only. Check " + uuid.uuid4().hex,
                        max_output_tokens=128,
                    )
                row.update(
                    probe="passed",
                    output=result.text[:100],
                    actual_provider=result.provider,
                    actual_model=result.model,
                    reported_cost_usd=result.cost_usd,
                    cache=result.cache,
                    exclusion=None,
                )
            except (OmniRouteError, TimeoutError) as exc:
                row.update(probe="failed", exclusion=str(exc) or "15 second timeout")
            print(row["route"], row["probe"], flush=True)

    await asyncio.gather(*(probe(row) for row in rows))
    data = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "total_exposed_ids": len(live),
        "total_catalogue_routes": len(rows),
        "plausible_free_labelled": sum(r["cost_class"] == "free_label" for r in rows),
        "plausible_zero_priced": sum(r["cost_class"] == "zero_priced" for r in rows),
        "probe_results": dict(Counter(r["probe"] for r in rows)),
        "rows": rows,
    }
    (ROOT / "outbound-requests.json").write_text(json.dumps(outbound, indent=2) + "\n")
    (ROOT / "live-audit.json").write_text(json.dumps(data, indent=2) + "\n")
    print({k: v for k, v in data.items() if k != "rows"})


asyncio.run(main())
