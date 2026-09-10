"""Bounded, zero-cost-only qualification of the current local catalogue."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from .models import RoutingProposal
from .omniroute import OmniRouteClient, OmniRouteError
from .recommendation import RecommendationCatalogue
from .tool_free import PROTOCOL_VERSION, execute


def free_cost_class(route: str, pricing: dict[str, Any]) -> str | None:
    """Require an explicit free route label or exact current zero token prices."""
    if route.endswith((":free", "-free")):
        return "free_label"
    provider, _, model = route.partition("/")
    prices = pricing.get(provider, {}).get(model, {})
    if not isinstance(prices, dict) or prices.get("mode", "chat") != "chat":
        return None
    if all(
        type(prices.get(key)) in {int, float} and prices[key] == 0 for key in ("input", "output")
    ) and all(prices.get(key, 0) == 0 for key in ("reasoning", "cached", "cache_creation")):
        return "zero_priced"
    return None


async def qualify_current_catalogue(
    client: OmniRouteClient, catalogue: RecommendationCatalogue, proposal: RoutingProposal
) -> dict[str, Any]:
    """Probe the actual protocol, without transmitting the user's task."""
    response = await client._request("GET", "/api/models/catalog")
    groups = response.body.get("catalog")
    if not isinstance(groups, dict):
        raise OmniRouteError("current catalogue did not contain provider groups")
    pricing = (await client._request("GET", "/api/pricing")).body
    forecasts = {row["provider_model_route_id"]: row for row in catalogue.forecasts}
    floor = proposal.recommendation.common_capability_floor if proposal.recommendation else 100
    rows: list[dict[str, Any]] = []
    for provider, group in groups.items():
        if not isinstance(group, dict) or group.get("active") is not True:
            continue
        for model in group.get("models", []):
            route = model.get("id") if isinstance(model, dict) else None
            if not isinstance(route, str):
                continue
            cost_class = free_cost_class(route, pricing)
            if cost_class is None:
                continue
            forecast = forecasts.get(route, {})
            lower = forecast.get("capability_score_lower")
            if not forecast.get("adviser_applicable") or not isinstance(lower, (int, float)):
                continue
            if lower < floor:
                continue
            rows.append(
                {
                    "provider": route.split("/", 1)[0],
                    "catalogue_provider": provider,
                    "route": route,
                    "cost_class": cost_class,
                    "context_tokens": model.get("context_length"),
                    "protocol": PROTOCOL_VERSION,
                    "probe": "not_probed",
                    "reasoning_effort": proposal.approved_constraints.reasoning_effort,
                    "exclusion": "outside bounded qualification shortlist",
                }
            )
    rows.sort(key=lambda row: (row["route"].split("/", 1)[0] != row["provider"], row["route"]))
    # Give each provider a turn before trying its second route or alias.
    pools: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pools.setdefault(row["provider"], []).append(row)
    shortlist = []
    while len(shortlist) < 40 and any(pools.values()):
        for pool in pools.values():
            if pool and len(shortlist) < 40:
                shortlist.append(pool.pop(0))
    semaphore = asyncio.Semaphore(6)

    async def probe(row: dict[str, Any]) -> None:
        async with semaphore:
            try:
                async with asyncio.timeout(30):
                    result = await execute(
                        client,
                        route=row["route"],
                        prompt=f"Reply OK only. Check {uuid.uuid4().hex}",
                        max_output_tokens=1024,
                        reasoning_effort=proposal.approved_constraints.reasoning_effort,
                    )
                row.update(
                    probe="passed",
                    actual_provider=result.provider,
                    actual_model=result.model,
                    reported_cost_usd=result.cost_usd,
                    cache=result.cache,
                    exclusion=None,
                )
            except (OmniRouteError, TimeoutError) as exc:
                row.update(probe="failed", exclusion=str(exc) or "qualification timed out")

    await asyncio.gather(*(probe(row) for row in shortlist))
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
