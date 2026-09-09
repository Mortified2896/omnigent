"""Explicit local protocol qualification; never run on ordinary user requests.

Usage: uv run python scripts/o3_qualify_tool_search.py --alias NAME --output FILE
The output is an operator-reviewed destination registry, not a model-name heuristic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from omnigent.server.o3_routing_review.omniroute import OmniRouteClient

SEARCH = {
    "type": "tool_search",
    "execution": "client",
    "description": "Find the diagnostic echo tool.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}
ECHO = {
    "type": "function",
    "name": "diagnostic_echo",
    "description": "Echo the supplied diagnostic text.",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    "defer_loading": True,
}


async def response(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    async with client.stream("POST", "/v1/responses", json=body) as reply:
        reply.raise_for_status()
        async for line in reply.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            event = json.loads(line[6:])
            if event.get("type") == "response.completed":
                return event["response"]
            if event.get("type") in {"response.failed", "error"}:
                raise ValueError("provider did not complete the protocol probe")
    raise ValueError("provider omitted a completed Responses event")


async def qualify(client: httpx.AsyncClient, route: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": route,
        "input": [
            {"type": "additional_tools", "role": "developer", "tools": [SEARCH]},
            {"role": "user", "content": "Search for the diagnostic echo tool before answering."},
        ],
        "stream": True,
        "store": False,
        "tool_choice": "auto",
        "reasoning": {"effort": "low"},
    }
    first = await response(client, body)
    search = next((item for item in first["output"] if item["type"] == "tool_search_call"), None)
    if search is None:
        raise ValueError("provider did not invoke client tool search")
    body["input"].extend(
        [
            *first["output"],
            {
                "type": "tool_search_output",
                "execution": "client",
                "call_id": search["call_id"],
                "status": "completed",
                "tools": [ECHO],
            },
            {"role": "user", "content": "Call diagnostic_echo with text LAZY_OK."},
        ]
    )
    second = await response(client, body)
    call = next((item for item in second["output"] if item["type"] == "function_call"), None)
    if call is None:
        raise ValueError("provider did not invoke the discovered function")
    if call["name"] != ECHO["name"] or json.loads(call["arguments"]) != {"text": "LAZY_OK"}:
        raise ValueError("discovered tool was not invoked with the expected arguments")
    if first["model"] != second["model"]:
        raise ValueError("destination changed during direct-route qualification")
    return {
        "resolved_model": second["model"],
        "supports_search_tool": True,
        "search_call": True,
        "loaded_tool_call": True,
        "evidence": "direct local Responses search-call and loaded-function continuation",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


async def main(alias: str, output: Path) -> None:
    omni = OmniRouteClient.from_env()
    combo = await omni.get_combo(alias)
    if combo is None:
        raise ValueError("alias does not exist")
    health = await omni._request("GET", "/api/monitoring/health")
    version = health.body.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("gateway omitted its transport version")
    live = await omni.model_ids()
    rows = {}
    async with httpx.AsyncClient(
        base_url=omni.base_url,
        headers={"Authorization": omni._authorization},
        timeout=90,
        trust_env=False,
    ) as client:
        for target in combo["models"]:
            route, provider = target["model"], target["providerId"]
            wire = route if route in live else f"{provider}/{route}"
            try:
                row = await qualify(client, wire)
            except (httpx.HTTPError, ValueError, KeyError, StopIteration) as exc:
                row = {"supports_search_tool": False, "evidence": type(exc).__name__}
            rows[route] = {"provider": provider, **row}
            print(route, row["supports_search_tool"], flush=True)
    # Exclusive creation preserves prior qualifications and their audit history.
    with output.open("x", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": 1,
                "gateway_url": omni.base_url,
                "gateway_version": version,
                "routes": rows,
            },
            handle,
            indent=2,
        )
        handle.write("\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.alias, args.output))
