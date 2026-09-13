"""Qualify one explicit Linux gateway route; never activates a routing registry."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from o3_qualify_tool_search import qualify

from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError
from omnigent.server.o3_routing_review.tool_free import PROTOCOL_VERSION, execute


async def main(route: str, kind: str, output: Path) -> None:
    if "/" not in route or route.startswith(("custom/", "auto/")):
        raise ValueError("qualification requires one exact provider/model route")
    if output.exists():
        raise FileExistsError("prior qualification is preserved")
    client = OmniRouteClient.from_env()
    record = {
        "schema_version": 1,
        "gateway_url": client.base_url,
        "route": route,
        "protocol": kind,
        "scope": "one explicit route; no combo, fallback, price or registry qualification",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with asyncio.timeout(45):
            health = await client._request("GET", "/api/monitoring/health")
            record["gateway_version"] = health.body["version"]
            if kind == "tool-search":
                async with httpx.AsyncClient(
                    base_url=client.base_url,
                    headers={"Authorization": client._authorization},
                    timeout=40,
                    trust_env=False,
                ) as http:
                    record["result"] = await qualify(http, route)
            else:
                result = await execute(
                    client,
                    route=route,
                    prompt="Reply RTX_TOOL_FREE_OK only.",
                    max_output_tokens=128,
                    reasoning_effort="low",
                )
                record["result"] = {
                    "protocol": PROTOCOL_VERSION,
                    "text_matches": result.text.strip() == "RTX_TOOL_FREE_OK",
                    "request_contract": result.request_contract,
                    "observed_provider": result.provider,
                    "observed_model": result.model,
                    "reported_cost_usd": result.cost_usd,
                    "response_id": result.response_id,
                }
                if result.text.strip() != "RTX_TOOL_FREE_OK":
                    raise ValueError("unexpected diagnostic answer")
            record["passed"] = True
    except (OmniRouteError, httpx.HTTPError, TimeoutError, ValueError, KeyError) as exc:
        record["passed"] = False
        record["error_class"] = type(exc).__name__
        # No provider body, credentials or user task is persisted.
    with output.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
    print(json.dumps(record))
    if not record["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", required=True)
    parser.add_argument("--kind", choices=("tool-search", "tool-free"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.route, args.kind, args.output))
