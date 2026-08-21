"""Rendered proof for stale-stream terminal reconciliation (#133)."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

_SESSION_ID = "conv_terminal_reconcile"
_AGENT_ID = "agent_terminal_reconcile"
_RESPONSE_ID = "resp_terminal_reconcile"
_SSE_HEARTBEAT = (
    "event: session.heartbeat\n"
    'data: {"type":"session.heartbeat","connection_id":"conn_visual",'
    '"sequence_number":0,"published_at":1234}\n\n'
)


def _fulfill_json(route: Route, body: Any) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(body),
    )


def _wait_for_stream_routes(page: Page, routes: list[Route], count: int) -> None:
    deadline = time.monotonic() + 10
    while len(routes) < count and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(routes) >= count


def test_running_drop_reconnect_and_terminal_snapshot_render_once(page: Page) -> None:
    """A dropped live terminal converges through the next durable snapshot."""
    base_url = os.environ["OMNIGENT_ISSUE_133_UI_BASE_URL"]
    evidence_dir = Path(os.environ.get("OMNIGENT_E2E_EVIDENCE_DIR", "test-results/issue-133"))
    evidence_dir.mkdir(parents=True, exist_ok=True)

    user_item = {
        "id": "msg_user",
        "response_id": _RESPONSE_ID,
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": "Finish this checkpoint."}],
    }
    assistant_item = {
        "id": "msg_assistant",
        "response_id": _RESPONSE_ID,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "model": "test-agent",
        "content": [{"type": "output_text", "text": "Checkpoint completed durably."}],
    }
    snapshot: dict[str, Any] = {
        "id": _SESSION_ID,
        "agent_id": _AGENT_ID,
        "agent_name": "test-agent",
        "status": "running",
        "active_response_id": _RESPONSE_ID,
        "created_at": 1_704_067_200,
        "updated_at": 1_704_067_200,
        "labels": {},
        "pending_elicitations": [],
        "pending_inputs": [],
    }
    items: list[dict[str, Any]] = [user_item]
    pending_stream_routes: list[Route] = []

    def route_api(route: Route) -> None:
        path = urlparse(route.request.url).path
        if path == f"/v1/sessions/{_SESSION_ID}/stream":
            pending_stream_routes.append(route)
            return
        if path == f"/v1/sessions/{_SESSION_ID}/items":
            newest_first = list(reversed(items))
            _fulfill_json(
                route,
                {
                    "object": "list",
                    "data": newest_first,
                    "first_id": newest_first[0]["id"] if newest_first else None,
                    "last_id": newest_first[-1]["id"] if newest_first else None,
                    "has_more": False,
                },
            )
            return
        if path == f"/v1/sessions/{_SESSION_ID}":
            _fulfill_json(route, snapshot)
            return
        if path == f"/v1/sessions/{_SESSION_ID}/agent":
            _fulfill_json(
                route,
                {
                    "id": _AGENT_ID,
                    "object": "agent",
                    "name": "test-agent",
                    "harness": "openai-agents",
                    "mcp_servers": [],
                    "policies": [],
                    "terminals": [],
                },
            )
            return
        if path.startswith(f"/v1/sessions/{_SESSION_ID}/"):
            _fulfill_json(route, {"object": "list", "data": [], "has_more": False})
            return
        if path == "/v1/sessions":
            _fulfill_json(route, {"object": "list", "data": [], "has_more": False})
            return
        if path == "/v1/sessions/projects":
            _fulfill_json(route, [])
            return
        if path == "/v1/agents":
            _fulfill_json(
                route,
                {
                    "data": [
                        {
                            "id": _AGENT_ID,
                            "name": "test-agent",
                            "display_name": "Test Agent",
                            "harness": "openai-agents",
                            "skills": [],
                        }
                    ]
                },
            )
            return
        if path == "/v1/hosts":
            _fulfill_json(route, {"hosts": []})
            return
        _fulfill_json(route, {"object": "list", "data": [], "has_more": False})

    page.route(re.compile(r".*/v1/.*"), route_api)
    page.route(
        re.compile(r".*/health(\?.*)?$"),
        lambda route: _fulfill_json(
            route,
            {"sessions": {_SESSION_ID: {"runner_online": True, "host_online": True}}},
        ),
    )
    page.route_web_socket(re.compile(r".*/v1/sessions/updates"), lambda ws: None)

    page.goto(f"{base_url}/c/{_SESSION_ID}")
    indicator = page.locator('[data-testid="working-indicator"]')
    expect(indicator).to_be_visible(timeout=30_000)
    expect(page.locator('[data-testid="message-bubble"][data-role="user"]')).to_have_count(1)
    page.screenshot(path=evidence_dir / "01-running.png", full_page=True)

    # The first established stream ends without [DONE]: transport lost before
    # the terminal/item frames were applied. Hold the next stream open long
    # enough to prove the browser-facing reconnect state.
    _wait_for_stream_routes(page, pending_stream_routes, 1)
    pending_stream_routes[0].fulfill(
        status=200,
        content_type="text/event-stream",
        body=_SSE_HEARTBEAT,
    )
    _wait_for_stream_routes(page, pending_stream_routes, 2)
    expect(indicator).to_contain_text("Reconnecting", timeout=10_000)
    page.screenshot(path=evidence_dir / "02-reconnecting.png", full_page=True)

    # Backend truth advances while the browser missed the live terminal/item.
    items.append(assistant_item)
    snapshot.update(
        {
            "status": "idle",
            "active_response_id": None,
            "terminal_response": {
                "response_id": _RESPONSE_ID,
                "status": "completed",
            },
        }
    )
    pending_stream_routes[1].fulfill(
        status=200,
        content_type="text/event-stream",
        body=f"{_SSE_HEARTBEAT}data: [DONE]\n\n",
    )

    final_bubble = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    expect(final_bubble).to_have_count(1, timeout=10_000)
    expect(final_bubble).to_contain_text("Checkpoint completed durably.")
    expect(indicator).to_have_count(0)
    expect(page.get_by_text("Checkpoint completed durably.", exact=True)).to_have_count(1)
    page.screenshot(path=evidence_dir / "03-reconciled-completed.png", full_page=True)
