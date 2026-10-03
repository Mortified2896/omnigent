"""Pointer reachability for the desktop composer's adjacent model selector."""

from __future__ import annotations

import re

import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.test_model_flows_prelaunch import _CLAUDE_HOST_ROWS
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
)


@pytest.mark.parametrize("width", [929, 1600])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_model_selector_stays_adjacent_and_reachable(
    seeded_session: tuple[str, str], width: int, theme: str
) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_adjacent_selector(base_url, session_id, width, theme))


async def _drive_adjacent_selector(base_url: str, session_id: str, width: int, theme: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": width, "height": 1000})
        page = await context.new_page()
        try:
            await _register_common_routes(page, created_session_id=session_id, create_bodies=[])
            await page.route(
                re.compile(r"/v1/sessions\?"),
                lambda route: route.fulfill(json={"data": [], "has_more": False}),
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                lambda route: route.fulfill(json={"models": _CLAUDE_HOST_ROWS}),
            )
            await page.add_init_script(
                f'localStorage.setItem("web-theme", "{theme}");'
                'localStorage.setItem("omnigent:ui-font-size", "16");'
            )
            await page.goto(f"{base_url}/")
            draft = page.get_by_test_id("new-chat-landing-input")
            await expect(draft).to_be_visible(timeout=30_000)
            await expect(draft).to_have_css("font-size", "16px")
            await expect(draft).to_have_css("line-height", "25.6px")
            trigger = page.get_by_test_id("new-chat-landing-agent-select")
            await trigger.click()
            harness = page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e")
            harness_row = harness.locator("xpath=ancestor::*[@data-harness-menu-row]")
            edit = page.get_by_test_id("new-chat-landing-model-select")
            await expect(harness).to_have_css("cursor", "pointer")
            await harness.hover()
            hovered_background = await harness_row.evaluate(
                "el => getComputedStyle(el).backgroundColor"
            )
            assert hovered_background != "rgba(0, 0, 0, 0)"
            other = page.get_by_test_id("new-chat-landing-custom-agents")
            await other.hover()
            await expect(harness_row).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            await expect(other).to_have_css("background-color", hovered_background)
            create_agent = page.get_by_test_id("new-chat-landing-create-agent")
            await expect(create_agent).to_be_visible()
            create_box = await create_agent.bounding_box()
            assert create_box is not None
            await page.mouse.move(
                create_box["x"] + create_box["width"] / 2,
                create_box["y"] + create_box["height"] / 2,
                steps=12,
            )
            await expect(other).to_have_attribute("data-state", "open")
            await expect(other).to_have_css("background-color", hovered_background)
            await expect(other).to_have_css("outline-style", "none")
            await expect(harness_row).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            await page.get_by_test_id("new-chat-landing-create-agent").press("ArrowLeft")
            await expect(other).to_have_attribute("data-state", "closed")
            menu = page.get_by_role("menu").first
            for gap in (
                menu.get_by_text("Harnesses", exact=True),
                menu.get_by_role("separator").first,
            ):
                await gap.hover()
                await expect(harness_row).to_have_css("background-color", "rgba(0, 0, 0, 0)")
                await expect(other).to_have_css("background-color", "rgba(0, 0, 0, 0)")
                await expect(other).to_have_css("outline-style", "none")
                await expect(other).to_have_attribute("data-state", "closed")
            menu_box = await menu.bounding_box()
            assert menu_box is not None
            await page.mouse.move(menu_box["x"] + menu_box["width"] / 2, menu_box["y"] + 3)
            await expect(harness_row).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            await expect(other).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            await page.mouse.move(20, 20)
            await expect(harness_row).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            await expect(harness).to_have_attribute("data-active", "true")
            # The model chip is a separate control: close the harness menu, then
            # open the model list from the chip itself.
            await page.keyboard.press("Escape")
            await expect(page.get_by_role("menu")).to_have_count(0)
            await edit.click()
            await expect(page.get_by_role("menu")).to_have_count(1)
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models).to_be_visible()
            child = models.locator('xpath=ancestor::*[@role="menu"]')
            await child.evaluate(
                "element => Promise.all("
                "element.getAnimations().map(animation => animation.finished))"
            )
            child_box = await child.bounding_box()
            trigger_box = await edit.bounding_box()
            assert child_box is not None and trigger_box is not None
            # The menu attaches BELOW the chip; Radix may overlap the trigger's
            # outer focus padding by at most 4px, so the menu must not cover
            # readable or clickable chip content.
            overlap = min(
                trigger_box["y"] + trigger_box["height"],
                child_box["y"] + child_box["height"],
            ) - max(trigger_box["y"], child_box["y"])
            assert overlap <= 4, (trigger_box, child_box)
            await expect(child).to_have_attribute("data-side", "bottom")

            # The menu stays open while the pointer travels inside it.
            await edit.hover()
            await models.hover()
            await page.wait_for_timeout(300)
            await expect(models).to_be_visible()
            target = models.get_by_role("menuitemcheckbox", name="Sonnet 5", exact=True)
            target_box = await target.bounding_box()
            assert target_box is not None
            await page.mouse.move(
                target_box["x"] + target_box["width"] / 2,
                target_box["y"] + target_box["height"] / 2,
                steps=20,
            )
            await expect(target).to_be_visible()
            await expect(target).to_have_attribute("data-highlighted", "")
            await page.mouse.click(
                target_box["x"] + target_box["width"] / 2,
                target_box["y"] + target_box["height"] / 2,
            )
            await expect(page.get_by_test_id("new-chat-landing-model-select")).to_have_text(
                "Sonnet 5"
            )
            await expect(page.get_by_role("menu")).to_have_count(1)
            await page.keyboard.press("Escape")
            await expect(page.get_by_role("menu")).to_have_count(0)
            await expect(edit).to_be_focused()
            # Shift+Tab steps back to the harness chip, the neighboring control.
            await edit.press("Shift+Tab")
            await expect(trigger).to_be_focused()
            await draft.click(position={"x": 10, "y": 10})
            await expect(page.get_by_role("menu")).to_have_count(0)
            await expect(draft).to_be_focused()
            await page.keyboard.type("continue typing")
            await expect(draft).to_have_value("continue typing")
            # The custom-agents entry stays reachable from the harness menu.
            await trigger.click()
            await other.click()
            await expect(page.get_by_role("menuitem", name="Create custom agent")).to_be_visible()
            await page.mouse.click(20, 20)
            await expect(page.get_by_role("menu")).to_have_count(0)
        finally:
            await context.close()
            await browser.close()
