"""E2E (hermetic): the new-session Codex effort menu omits Default.

The inline new-session effort menu is driven by the selected Codex model's
live ``supportedReasoningEfforts`` catalog. It must keep catalog-specific
levels, including levels added by newer Codex versions, while omitting the
non-specific ``default`` sentinel.

The driving surface is the real SPA in a browser; only the server edges the
landing screen consults (hosts, agents, model-options) are faked, exactly like
the sibling tests in ``test_start_session.py``. The stubbed Codex catalog
includes the non-specific ``default`` entry alongside model-specific efforts;
the mobile test confirms only the sentinel is removed and explicit levels
still reach the create request.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _open_entry_config,
    _pick_config_select,
    _register_common_routes,
    _run_in_fresh_loop,
    _save_config,
    _wait_until,
)

# Host catalog rows for codex-native, shaped like Codex's own ``model/list``
# payload: each model advertises its effort ladder, exactly as the in-session
# snapshot's ``model_options`` do (see tests/e2e_ui/chat/
# test_codex_model_metadata.py).
_CODEX_HOST_ROWS = [
    {
        "id": "gpt-live-default",
        "displayName": "GPT Live Default",
        "isDefault": True,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "default", "description": "Default"},
            {"reasoningEffort": "low", "description": "Fastest"},
            {"reasoningEffort": "medium", "description": "Balanced"},
            {"reasoningEffort": "high", "description": "Most thorough"},
        ],
    },
    {
        "id": "gpt-live-fast",
        "displayName": "GPT Live Fast",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "default", "description": "Default"},
            {"reasoningEffort": "low", "description": "Fastest"},
            {"reasoningEffort": "medium", "description": "Balanced"},
            {"reasoningEffort": "high", "description": "Most thorough"},
        ],
    },
]


def test_new_codex_session_effort_omits_default_and_keeps_catalog_levels(
    seeded_session: tuple[str, str],
) -> None:
    """The inline menu uses the selected model's explicit catalog efforts.

    A ``default`` row in the model catalog is not a specific reasoning level,
    so the picker omits it. Model-specific levels remain selectable and the
    selected one reaches the new-session request.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_codex_effort_prelaunch(base_url, session_id))


async def _drive_codex_effort_prelaunch(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so the `finally` can close IT before the browser —
        # closing only the browser can drop an in-flight video recording
        # (OMNIGENT_E2E_RECORD_DIR) on the floor as a 0-byte file.
        context = await browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
        )
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )

            # Neutralize agent discovery so ONLY the stubbed Codex agent feeds
            # the picker — a native agent another test left behind on the
            # shared server would rank ahead, auto-select, and the gear would
            # open the wrong agent's config modal.
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
                    body=json.dumps({"models": _CODEX_HOST_ROWS}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
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
            await _open_entry_config(page, "ag_codex_e2e")

            modal = page.get_by_test_id("new-chat-landing-config-modal")
            await expect(modal).to_be_visible()
            # The Model row names the catalog's default id, confirming that the
            # host catalog response also supplies the effort ladder.
            model = page.get_by_test_id("new-chat-landing-config-model")
            await expect(model).to_contain_text("Default (gpt-live-default)")
            await _pick_config_select(page, "new-chat-landing-config-model", "gpt-live-fast")
            await _save_config(page)

            # This compact menu above the composer is the phone-visible
            # selector. It uses the same selected-model catalog ladder.
            effort = page.get_by_test_id("new-chat-landing-inline-effort")
            await expect(effort).to_be_visible()
            await effort.click()
            await expect(
                page.get_by_role("option", name=re.compile(r"^default$", re.IGNORECASE))
            ).to_have_count(0)
            option = page.get_by_role("option", name=re.compile(r"^high$", re.IGNORECASE))
            await expect(option).to_be_visible()
            await option.click()
            await expect(effort).to_contain_text(re.compile(r"high", re.IGNORECASE))

            # The specific pick must reach the new-session request.
            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_codex_e2e", body
            assert body.get("model_override") == "gpt-live-fast", body
            assert body.get("reasoning_effort") == "high", body
        finally:
            await context.close()
            await browser.close()
