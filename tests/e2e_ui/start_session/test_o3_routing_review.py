"""The customized composer must not reactivate the retired O3 review entry."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from playwright.async_api import Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _SESSIONS_RE,
    _run_in_fresh_loop,
)

_PROPOSAL_ID = "01234567-89ab-cdef-0123-456789abcdef"
_DERIVED_COMBO = "custom/o3-route-0123456789ab"
_PROMPT = "Inspect the routing layer without changing files."


def _proposal(
    *,
    effort: str = "low",
    minimum_score: float = 0.5,
    decision: str | None = None,
    derived_combo: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": 2,
        "constraint_version": 1,
        "requirement_overrides": {},
        "proposal_id": _PROPOSAL_ID,
        "created_at": "2026-09-04T00:00:00Z",
        "updated_at": "2026-09-04T00:01:00Z",
        "expires_at": "2026-09-05T00:00:00Z",
        "prompt_fingerprint": "sha256:" + "0" * 64,
        "workspace_summary": "Workspace: /work/repo",
        "adviser": {
            "task_summary": "Inspect the routing layer without mutating the repository",
            "task_classification": "systems",
            "difficulty": "normal",
            "risk": "low",
            "requirements": {
                "terminal": True,
                "tools": True,
                "minimum_context_tokens": 0,
                "vision": False,
            },
            "benchmark_requirements": [
                {
                    "benchmark_id": "terminal-bench",
                    "version": "4.0.0",
                    "slice_id": "tb4.cr-systems-db-v1",
                    "minimum_score": minimum_score,
                    "reason": "Terminal and repository inspection competence are required.",
                }
            ],
            "proposed_reasoning_effort": effort,
            "evidence_policy": "provisional",
            "disposition": "borderline",
            "confidence": 0.8,
            "rationale": "A tool-capable route is required; exact benchmark evidence is absent.",
            "decomposition": [],
        },
        "approved_constraints": {
            "difficulty": "normal",
            "calibration_version": "test",
            "benchmark": {
                "benchmark_id": "terminal-bench",
                "version": "4.0.0",
                "slice_id": "tb4.cr-systems-db-v1",
                "minimum_score": minimum_score,
                "reason": "Terminal and repository inspection competence are required.",
            },
            "reasoning_effort": effort,
            "risk": "low",
            "evidence_policy": "provisional",
            "cost_quota_preference": "preserve_subscription",
        },
        "evaluations": [
            {
                "candidate": {
                    "candidate_id": "codex-gpt-5-5-low",
                    "provider_id": "codex",
                    "model": "gpt-5.5",
                    "catalogue_model_id": "codex/gpt-5.5",
                    "harness": "codex-native",
                    "supported_reasoning_efforts": ["low", "high"],
                    "context_tokens": 128000,
                    "terminal": True,
                    "tools": True,
                    "vision": False,
                    "responses_api": True,
                    "monetary_cost_usd": None,
                    "cost_source": "Codex subscription",
                    "quota_source": "Codex rolling quota",
                    "last_full_probe_at": "2026-09-03T00:00:00+08:00",
                    "probe_reference": "live Responses tool-continuation probe",
                    "provider_usable": True,
                    "model_present": True,
                    "quota_available": True,
                    "quota_remaining_percent": 80,
                    "quota_reset_at": None,
                    "recent_success_rate": 1.0,
                    "recent_retry_rate": 0.0,
                    "latency_ms": 500,
                },
                "status": "provisional",
                "evidence_class": "proxy",
                "admission_score": 0.62,
                "evidence": [],
                "exclusions": [],
                "caveats": ["No exact benchmark evidence exists for this execution path."],
                "ranking": {
                    "evidence_confidence": 0.5,
                    "competence_margin": 0.12,
                    "health": 1.0,
                    "estimated_monetary_cost_usd": None,
                    "quota_remaining_percent": 80,
                    "quota_reset_at": None,
                    "quota_scarcity_penalty": 0.2,
                    "recent_failure_rate": 0.0,
                    "recent_retry_rate": 0.0,
                    "latency_ms": 500,
                    "deterministic_score": 0.7,
                },
            }
        ],
        "frontier": {
            "requested_minimum": minimum_score,
            "global_measured_frontier": 0.7,
            "accessible_configured_frontier": 0.62,
            "healthy_available_frontier": 0.62,
            "passing_exact_candidates": [],
            "provisional_candidates": ["codex-gpt-5-5-low"],
            "capability_gap": "No currently accessible configuration has exact evidence.",
        },
        "disposition": "borderline",
        "decision": decision,
        "decision_reason": None,
        "derived_combo_name": derived_combo,
        "derived_combo_definition": (
            {"name": derived_combo, "models": [{"providerId": "codex", "model": "gpt-5.5"}]}
            if derived_combo
            else None
        ),
        "session_id": session_id,
        "actual_provider": None,
        "actual_model": None,
        "actual_reasoning_effort": None,
        "execution_provenance": [],
        "execution_status": None,
        "provenance_synced_at": None,
        "task_outcome": None,
        "terminal_disposition": None,
    }

    result["original_adviser"] = dict(result["adviser"])
    result["adviser_mode"] = "model"
    result["adviser_exchanges"] = [
        {
            "requested_model": "reviewer-combo",
            "actual_model": "reviewer-model",
            "actual_provider": "reviewer-provider",
            "reasoning_effort": "medium",
            "transmitted_model": "reviewer-combo",
            "transmitted_effort": "medium",
            "harness": "Responses API via OmniRoute",
            "duration_ms": 7400,
            "request": {
                "input": [
                    {"role": "system", "content": "Exact instructions"},
                    {"role": "user", "content": _PROMPT},
                ]
            },
            "response_text": "Complete provider output\n"
            + "Returned rationale, uncertainty, notes.\n" * 80,
            "response": {
                "output_text": "Complete parsed JSON",
                "unknown_future_field": [1, 2],
                "reasoning": {"summary": "Returned reasoning"},
                "usage": {"input_tokens": 1842},
            },
            "explanation": "Parsed rationale",
            "reasoning_summary": "Returned reasoning",
            "attempt": 1,
        }
    ]
    result["audit"] = {
        "version": 1,
        "calibration_rule": "benchmark + difficulty -> threshold",
        "initial_constraints": result["approved_constraints"],
        "history": [],
    }
    return result


async def _register_routes(
    page: Page,
    *,
    created_session_id: str,
    create_bodies: list[dict[str, Any]],
    adjustment_bodies: list[dict[str, Any]],
    decision_bodies: list[dict[str, Any]],
    link_bodies: list[dict[str, Any]],
    event_bodies: list[dict[str, Any]],
) -> None:
    await page.route(
        re.compile(r"/v1/skills(?:\?.*)?$"),
        lambda route: route.fulfill(json={"data": []}),
    )
    current = _proposal()
    current["original_adviser"] = dict(current["adviser"])

    async def handle_info(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "accounts_enabled": False,
                    "single_user": True,
                    "needs_setup": False,
                    "smart_routing_enabled": False,
                    "o3_routing_review_enabled": True,
                }
            ),
        )

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": "e2e-host",
                            "owner": "e2e",
                            "status": "online",
                        }
                    ]
                }
            ),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "data": [
                        {
                            "id": "ag_codex_e2e",
                            "name": "codex-native-ui",
                            "display_name": "Codex",
                            "description": "OpenAI Codex harness",
                            "harness": "codex-native",
                            "skills": [],
                        }
                    ]
                }
            ),
        )

    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    async def handle_model_options(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"models": []}),
        )

    async def handle_worktrees(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"object": "list", "data": []}),
        )

    async def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            create_bodies.append(route.request.post_data_json)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id}),
            )
        else:
            await route.continue_()

    async def handle_events(route: Route) -> None:
        event_bodies.append(route.request.post_data_json)
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_o3_e2e"}),
        )

    async def handle_o3(route: Route) -> None:
        path = urlparse(route.request.url).path
        if "/session/" in path:
            body = [current]
            status = 200
        elif path.endswith("/registry"):
            body: object = {
                "source_pool": "custom/o3-codex-pool",
                "slices": [
                    {
                        "benchmark_id": "terminal-bench",
                        "version": "4.0.0",
                        "slice_id": "tb4.cr-systems-db-v1",
                        "label": "Systems and databases",
                        "interpretation": "Repository and terminal work",
                        "task_ids": [],
                        "task_manifest_digest": "sha256:e2e",
                        "official": False,
                    }
                ],
            }
            status = 200
        elif path.endswith("/proposals") and route.request.method == "POST":
            await asyncio.sleep(1.5)
            body = _proposal()
            status = 201
        elif path.endswith(f"/{_PROPOSAL_ID}") and route.request.method == "PATCH":
            adjustment_bodies.append(route.request.post_data_json)
            patch = route.request.post_data_json
            if "requirement_overrides" in patch:
                for field, value in patch["requirement_overrides"].items():
                    if value is None or value == current["adviser"]["requirements"][field]:
                        current["requirement_overrides"].pop(field, None)
                    else:
                        current["requirement_overrides"][field] = value
                current["effective_requirements"] = {
                    **current["adviser"]["requirements"],
                    **current["requirement_overrides"],
                }
                current["constraint_version"] += 1
                body = current
            else:
                current["approved_constraints"]["reasoning_effort"] = (
                    "low" if patch.get("reset_reasoning_effort") else patch["reasoning_effort"]
                )
                current["constraint_version"] += 1
                current["decision"] = None
                body = current
            status = 200
        elif path.endswith(f"/{_PROPOSAL_ID}") and route.request.method == "GET":
            body = current
            status = 200
        elif path.endswith("/decision"):
            decision_bodies.append(route.request.post_data_json)
            if route.request.post_data_json["action"] == "wait":
                current["decision"] = "wait"
                body = current
            else:
                body = _proposal(
                    effort="high",
                    minimum_score=0.5,
                    decision="approve",
                    derived_combo=_DERIVED_COMBO,
                )
            status = 200
        elif path.endswith("/session"):
            link_bodies.append(route.request.post_data_json)
            body = _proposal(
                effort="high",
                minimum_score=0.5,
                decision="approve",
                derived_combo=_DERIVED_COMBO,
                session_id=created_session_id,
            )
            status = 200
        else:
            raise AssertionError(f"unexpected O3 request: {route.request.method} {path}")
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    await page.route(
        "**/resources/terminals?*",
        lambda route: route.fulfill(status=200, json={"data": []}),
    )
    await page.route(
        "**/resources/environments/default",
        lambda route: route.fulfill(status=200, json={}),
    )
    await page.route("**/v1/info", handle_info)
    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
    await page.route(
        re.compile(r"/v1/hosts/[^/]+/harnesses/[^/]+/model-options$"),
        handle_model_options,
    )
    await page.route(re.compile(r"/v1/hosts/[^/]+/worktrees(?:\?.*)?$"), handle_worktrees)
    await page.route(re.compile(r"/v1/o3/routing-review(?:/.*)?$"), handle_o3)
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


@pytest.mark.parametrize(
    "viewport", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}]
)
def test_retired_o3_review_is_not_offered_even_with_legacy_server_flag(
    seeded_session: tuple[str, str],
    tmp_path: Path,
    viewport: dict[str, int],
) -> None:
    """A legacy server flag cannot restore the removed routing/judge flow."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_o3_review(base_url, session_id, viewport, tmp_path))


async def _drive_o3_review(
    base_url: str, session_id: str, viewport: dict[str, int], evidence: Path
) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport=viewport)
        try:
            create_bodies: list[dict[str, Any]] = []
            adjustment_bodies: list[dict[str, Any]] = []
            decision_bodies: list[dict[str, Any]] = []
            link_bodies: list[dict[str, Any]] = []
            event_bodies: list[dict[str, Any]] = []
            console_errors: list[str] = []
            page_errors: list[str] = []
            http_errors: list[str] = []
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "response",
                lambda response: (
                    http_errors.append(
                        f"{response.status} {response.request.method} {response.url}"
                    )
                    if response.status >= 400
                    else None
                ),
            )
            await _register_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                adjustment_bodies=adjustment_bodies,
                decision_bodies=decision_bodies,
                link_bodies=link_bodies,
                event_bodies=event_bodies,
            )

            await page.goto(f"{base_url}/")
            composer = page.get_by_test_id("new-chat-landing-input")
            await composer.wait_for(state="visible", timeout=30_000)
            await composer.fill(_PROMPT)
            await expect(page.get_by_test_id("new-chat-landing-inline-model")).to_have_count(0)
            await expect(page.get_by_text("Benchmark Routing (O3)", exact=True)).to_have_count(0)
            await expect(page.get_by_test_id("o3-routing-proposal-card")).to_have_count(0)
            await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
            await page.screenshot(path=str(evidence / "retired-o3-entry.png"), full_page=True)
            assert create_bodies == []
            assert adjustment_bodies == []
            assert decision_bodies == []
            assert link_bodies == []
            assert event_bodies == []
            assert not page_errors, page_errors
            assert not http_errors, http_errors
            assert not console_errors, console_errors
        finally:
            await browser.close()
