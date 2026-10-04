"""E2E: a resolved Codex lane row without effort tiers still gets a ladder.

Live regression (2026-10-04): the OmniRoute gateway serves GLM routes like
``glm/glm-5-turbo`` without ``effort_tiers``, so the picker rows carry no
``supportedReasoningEfforts`` and the landing hid the reasoning selector for
exactly the owner's state (Codex harness + GLM 5 Turbo · OmniRoute). The row
resolves, so the picker must offer the conservative baseline rungs instead of
hiding, and the model trigger must not wear the harness/agent icon.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_glm_row_without_effort_tiers_keeps_reasoning_selector(
    seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive(base_url, session_id, tmp_path))


async def _drive(base_url: str, session_id: str, tmp_path: Path) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1000})
        create_bodies: list[dict[str, object]] = []
        try:
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
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
                                    "accessLane": "omniroute",
                                    "groupLabel": "OmniRoute",
                                    "isDefault": True,
                                    "supportedReasoningEfforts": [
                                        {"reasoningEffort": "low"},
                                        {"reasoningEffort": "medium"},
                                        {"reasoningEffort": "high"},
                                        {"reasoningEffort": "xhigh"},
                                    ],
                                },
                                {
                                    # The exact live shape of the OmniRoute GLM
                                    # row: resolved, lane-stamped, and carrying
                                    # NO supportedReasoningEfforts.
                                    "id": "glm/glm-5-turbo",
                                    "model": "glm/glm-5-turbo",
                                    "displayName": "GLM 5 Turbo · OmniRoute",
                                    "accessLane": "omniroute",
                                    "groupLabel": "GLM",
                                },
                            ]
                        }
                    ),
                )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body='{"data": []}'
                )

            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                handle_agent_scan,
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
                handle_model_options,
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

            # The harness chip keeps its agent icon; the model trigger must
            # not inherit it (a Codex logo on a GLM model is wrong).
            await expect(page.get_by_test_id("new-chat-landing-agent-icon")).to_be_visible()
            await expect(page.get_by_test_id("new-chat-landing-model-icon")).to_have_count(0)

            # Open the direct model picker and choose the GLM row.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-model-select").click()
            menu = page.get_by_test_id("new-chat-landing-model-menu")
            await menu.wait_for(state="visible", timeout=10_000)
            await page.get_by_role(
                "menuitemcheckbox", name="GLM 5 Turbo", exact=True
            ).first.click()
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)

            model_chip = page.get_by_test_id("new-chat-landing-model-select")
            await expect(model_chip).to_contain_text("GLM 5 Turbo")

            # The reasoning selector stays visible with the baseline rungs.
            effort = page.get_by_test_id("new-chat-landing-inline-effort")
            await expect(effort).to_be_visible()
            await expect(effort).to_contain_text("Default")
            await effort.click()
            for rung in ("Low", "Medium", "High"):
                await expect(page.get_by_role("option", name=rung, exact=True)).to_be_visible()
            await expect(page.get_by_role("option", name="XHigh", exact=True)).to_have_count(0)
            await page.get_by_role("option", name="High", exact=True).click()
            await expect(effort).to_contain_text("High")

            # The chosen model + effort ride the create request.
            await page.get_by_test_id("new-chat-landing-input").fill("GLM ladder check")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(re.compile(r"/c/temp"))
            assert create_bodies, "session create was not issued"
            assert create_bodies[0]["model_override"] == "glm/glm-5-turbo", create_bodies
            assert create_bodies[0]["reasoning_effort"] == "high", create_bodies

            await page.screenshot(path=tmp_path / "glm-effort-ladder.png", animations="disabled")
        finally:
            await page.unroute_all(behavior="wait")
            await browser.close()
