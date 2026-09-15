"""Read-only acceptance against completed sessions in the actual local Mac app.

Set O3_LAZY_SESSION_ID, O3_EAGER_SESSION_ID and O3_DISCOVERY_SESSION_ID and O3_CODING_SESSION_ID to
fresh native sessions created with normal MCP configuration before running.
"""

import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from omnigent.codex_native_app_server import CodexAppServerClient

_IDS = [os.getenv(f"O3_{name}_SESSION_ID") for name in ["EAGER", "LAZY", "DISCOVERY", "CODING"]]
pytestmark = pytest.mark.skipif(not all(_IDS), reason="requires four local Mac native sessions")


async def _inspect(session_id):
    async with httpx.AsyncClient(base_url="http://127.0.0.1:6768", trust_env=False) as client:
        response = await client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        session = response.json()
    bridge = (
        Path.home()
        / ".omnigent/codex-native"
        / hashlib.sha256(session_id.encode()).hexdigest()[:32]
    )
    state = json.loads((bridge / "state.json").read_text())
    client = CodexAppServerClient(ws_url=state["socket_path"], client_name="o3-lazy-acceptance")
    await client.connect()
    tools = set()
    try:
        cursor = None
        while True:
            args = {"detail": "toolsAndAuthOnly", "limit": 100}
            if cursor:
                args["cursor"] = cursor
            result = (await client.request("mcpServerStatus/list", args))["result"]
            for server in result["data"]:
                tools.update((server["name"], name) for name in server.get("tools", {}))
            cursor = result.get("nextCursor")
            if not cursor:
                break
    finally:
        await client.close()
    records = []
    for file in (bridge / "codex-home/sessions").glob("**/*.jsonl"):
        records.extend(json.loads(line) for line in file.read_text().splitlines())
    return session, tools, records


async def test_local_native_catalogue_is_preserved_and_specialists_are_discovered():
    eager, lazy, discovery, coding = [await _inspect(session_id) for session_id in _IDS]
    assert eager[1] == lazy[1] == discovery[1] == coding[1]
    assert any(server == "omniroute" for server, _ in lazy[1])
    assert eager[0]["external_session_id"] != lazy[0]["external_session_id"]
    usage = [r["payload"]["info"] for r in lazy[2] if r["payload"].get("type") == "token_count"]
    assert usage[0]["last_token_usage"]["input_tokens"] < 30000
    assert lazy[0]["context_window"] == eager[0]["context_window"]
    assert not any(r["payload"].get("type") == "tool_search_call" for r in lazy[2])
    calls = [r["payload"] for r in discovery[2] if r["type"] == "response_item"]
    assert any(c.get("type") == "tool_search_call" for c in calls)
    assert any(
        c.get("type") == "function_call" and "omniroute" in c.get("name", "") for c in calls
    )
    assert discovery[0]["status"] == "idle"

    from omnigent.server.o3_routing_review.omniroute import OmniRouteClient

    omni = OmniRouteClient.from_env()
    logs = await omni.list_call_logs(limit=1000, offset=0)
    for checked in [lazy, coding]:
        first = next(
            r["payload"]["info"]["last_token_usage"]["input_tokens"]
            for r in checked[2]
            if r["payload"].get("type") == "token_count"
        )
        request = None
        for row in logs:
            if row.get("tokens", {}).get("in") != first:
                continue
            detail = await omni._request("GET", "/api/usage/call-logs/" + row["id"])
            body = detail.body.get("requestBody")
            if (
                isinstance(body, dict)
                and body.get("prompt_cache_key") == checked[0]["external_session_id"]
            ):
                request = body
                break
        assert request is not None, "fresh native request must be inspectable"
        tools = list(request.get("tools", []))
        for item in request.get("input", []):
            if item.get("type") == "additional_tools":
                tools.extend(item.get("tools", []))
        assert any(tool.get("type") == "tool_search" for tool in tools)
        assert not any(tool.get("name", "").startswith("mcp__omniroute") for tool in tools)
    loaded = [c for c in calls if c.get("type") == "tool_search_output"]
    loaded_omniroute = {
        tool["name"]
        for call in loaded
        for namespace in call.get("tools", [])
        if namespace.get("name") == "mcp__omniroute"
        for tool in namespace.get("tools", [])
    }
    assert 0 < len(loaded_omniroute) < sum(server == "omniroute" for server, _ in discovery[1])
