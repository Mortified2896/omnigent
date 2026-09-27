"""E2E: the advisor uses the normal Codex composer choice as the proposal."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)

_OPENAI_CHOICE_ID = "logical-openai-gpt-5.5-medium"
_GLM_CHOICE_ID = "logical-glm-glm-5.3-high"
_CATALOG = {
    "object": "model_advisor.catalog",
    "catalog_revision": "e2e-catalog-v1",
    "options": [],
    "logical_options": [
        {
            "choice_id": _OPENAI_CHOICE_ID,
            "provider": "openai",
            "model_id": "gpt-5.5",
            "display_name": "GPT-5.5",
            "reasoning_effort": "medium",
            "model_ids": ["gpt-5.5"],
            "access_lanes": ["codex-direct"],
            "default_access_lanes": ["codex-direct"],
            "available": True,
        },
        {
            "choice_id": _GLM_CHOICE_ID,
            "provider": "glm",
            "model_id": "glm-5.3",
            "display_name": "GLM-5.3",
            "reasoning_effort": "high",
            "model_ids": ["glm-5.3"],
            "access_lanes": ["glm-direct"],
            "default_access_lanes": ["glm-direct"],
            "available": True,
        },
    ],
}
_SAVED_PREFERENCES = {
    "schema_version": 3,
    "enabled": False,
    "providers": {
        "openai": {
            "enabled": True,
            "collapsed": False,
            "selected_choice_ids": [_OPENAI_CHOICE_ID],
            "disabled_model_ids": [],
            "transport_preference": "omniroute_preferred",
        },
        "glm": {
            "enabled": True,
            "collapsed": False,
            "selected_choice_ids": [_GLM_CHOICE_ID],
            "disabled_model_ids": [],
            "transport_preference": "direct_only",
        },
    },
    "advisor_choice_id": _GLM_CHOICE_ID,
    "human_probability_percent": 50,
    "unresolved_legacy_ids": [],
    "route_review_required": [],
}


def test_model_advisor_composer_pick_becomes_human_proposal(
    seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """Keep the composer selectors visible and submit their logical proposal."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_composer_proposal(base_url, session_id, tmp_path))


async def _drive_composer_proposal(base_url: str, session_id: str, tmp_path: Path) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
        )
        page = await context.new_page()
        try:
            round_posts: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=[],
                agents_body=_codex_native_agents_body(),
            )

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
                            "features": {"model_advisor": True},
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
                    body=json.dumps(
                        {
                            "models": [
                                {
                                    "id": "gpt-5.5",
                                    "model": "gpt-5.5",
                                    "displayName": "GPT-5.5",
                                    "accessLane": "codex-direct",
                                    "isDefault": True,
                                    "defaultReasoningEffort": "medium",
                                    "supportedReasoningEfforts": [
                                        {"reasoningEffort": "low"},
                                        {"reasoningEffort": "medium"},
                                    ],
                                }
                            ]
                        }
                    ),
                )

            async def handle_advisor_catalog(route: Route) -> None:
                await route.fulfill(status=200, content_type="application/json", json=_CATALOG)

            async def handle_advisor_preferences(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    json={
                        "object": "model_advisor.preferences",
                        "version": 1,
                        "etag": '"e2e-preferences"',
                        "state": "saved",
                        "preferences": _SAVED_PREFERENCES,
                        "logical_preferences": _SAVED_PREFERENCES,
                    },
                )

            async def handle_advisor_round(route: Route) -> None:
                if route.request.method == "POST":
                    round_posts.append(route.request.post_data_json)
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    json={
                        "object": "model_advisor.round",
                        "round_id": "adviseround-e2e",
                        "state": "awaiting_confirmation",
                        "version": 1,
                        "etag": '"round-e2e"',
                        "failure_reason": None,
                        "execution": {"session_id": None, "uncertain": False},
                        "review": {
                            "round_fingerprint": "fp-e2e",
                            "human_choice_id": _OPENAI_CHOICE_ID,
                            "advisor_choice_id": _GLM_CHOICE_ID,
                            "rationale": "The GLM checkpoint is a good comparison.",
                            "assigned_choice_id": _OPENAI_CHOICE_ID,
                            "assigned_arm": "human",
                            "human_probability_percent": 50,
                            "overridden": False,
                            "override_reason": None,
                            "comparison_group": "randomized_unblinded",
                        },
                    },
                )

            await page.route("**/v1/info", handle_info)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
                handle_model_options,
            )
            await page.route("**/v1/model-advisor/catalog?*", handle_advisor_catalog)
            await page.route("**/v1/model-advisor/preferences?*", handle_advisor_preferences)
            await page.route(
                re.compile(r"/v1/model-advisor/rounds(?:/[^/?]+)?(?:\?.*)?$"),
                handle_advisor_round,
            )
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-agent-ag_codex_e2e").click()

            advisor_switch = page.get_by_role("switch", name="Compare my choice with the advisor")
            await expect(advisor_switch).to_be_visible()
            await advisor_switch.click()
            await expect(
                page.get_by_text("Allowed answers — shared by you and the advisor")
            ).to_be_visible()
            await expect(page.get_by_test_id("new-chat-landing-inline-model")).to_be_visible()
            await expect(page.get_by_test_id("new-chat-landing-inline-effort")).to_be_visible()
            await expect(page.get_by_test_id("model-advisor-advisor-choice")).to_be_visible()
            await expect(page.get_by_text("Your model and reasoning", exact=False)).to_have_count(
                0
            )

            model_picker = page.get_by_test_id("new-chat-landing-inline-model")
            agent_picker = page.get_by_test_id("new-chat-landing-agent-select")
            gear_picker = page.get_by_test_id("new-chat-landing-config-gear")
            await model_picker.click()
            await page.locator(
                '[role="option"][data-model-id="gpt-5.5"][data-access-lane="codex-direct"]'
            ).click()
            effort_picker = page.get_by_test_id("new-chat-landing-inline-effort")
            agent_box = await agent_picker.bounding_box()
            gear_box = await gear_picker.bounding_box()
            model_box = await model_picker.bounding_box()
            effort_box = await effort_picker.bounding_box()
            assert (
                agent_box is not None
                and gear_box is not None
                and model_box is not None
                and effort_box is not None
            )
            assert abs(agent_box["y"] - model_box["y"]) < 3
            assert abs(gear_box["y"] - model_box["y"]) < 3
            assert abs(model_box["y"] - effort_box["y"]) < 2
            assert model_box["width"] <= 112
            advisor_picker = page.get_by_test_id("model-advisor-advisor-choice")
            advisor_box = await advisor_picker.bounding_box()
            assert advisor_box is not None and model_box["y"] < advisor_box["y"]
            assert (
                await page.get_by_test_id("model-advisor-composer-choice")
                .get_by_test_id("model-advisor-advisor-choice")
                .count()
                == 1
            )
            await effort_picker.click()
            await page.get_by_role("option", name="Medium", exact=True).click()
            await expect(model_picker).to_contain_text("GPT-5.5")
            await expect(effort_picker).to_contain_text("Medium")

            await page.screenshot(
                path=str(tmp_path / "model-advisor-composer-mobile.png"), full_page=True
            )
            await page.get_by_test_id("model-advisor-advisor-choice").scroll_into_view_if_needed()
            await page.screenshot(path=str(tmp_path / "model-advisor-advisor-picker-mobile.png"))
            await page.set_viewport_size({"width": 1280, "height": 900})
            await expect(page.get_by_test_id("new-chat-landing-inline-model")).to_be_visible()
            await expect(page.get_by_test_id("new-chat-landing-inline-effort")).to_be_visible()
            await page.screenshot(path=str(tmp_path / "model-advisor-composer-desktop.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_test_id("new-chat-landing-input").fill("Compare the selected models")
            await page.get_by_role("button", name="Get recommendation").click()
            await expect(
                page.get_by_text("The GLM checkpoint is a good comparison.")
            ).to_be_visible()
            assert len(round_posts) == 1
            assert round_posts[0]["human_choice_id"] == _OPENAI_CHOICE_ID
            assert round_posts[0]["preferences"]["schema_version"] == 3
        finally:
            await context.close()
            await browser.close()
