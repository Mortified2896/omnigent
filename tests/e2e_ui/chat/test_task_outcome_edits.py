"""Unsaved review details survive outcome changes on a durable answer."""

from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn


def test_outcome_change_preserves_unsaved_details(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="Review this completed fixture task.",
        reply="The fixture task is complete.",
    )
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    partial = page.get_by_role("button", name="Partial", exact=True)
    expect(partial).to_be_enabled(timeout=30_000)
    partial.click()
    comment = page.get_by_role("textbox", name="Task review comment")
    expect(comment).to_be_visible()
    comment.fill("Verified the requested result.")
    page.get_by_role("button", name="Tests/verification", exact=True).click()
    page.get_by_role("textbox", name="Custom task review tag").fill("Regression")
    page.get_by_role("button", name="Add tag", exact=True).click()
    success = page.get_by_role("button", name="Success", exact=True)
    success.click()
    expect(success).to_have_attribute("aria-pressed", "true")
    page.reload()
    expect(page.get_by_role("textbox", name="Task review comment")).to_have_value(
        "Verified the requested result.", timeout=30_000
    )
    expect(page.get_by_role("button", name="Tests/verification", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.get_by_role("button", name="Regression ×", exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / "outcome-details.png"), full_page=True)
