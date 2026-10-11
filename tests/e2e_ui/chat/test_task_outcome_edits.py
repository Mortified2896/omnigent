"""Unsaved review details survive outcome changes on a durable answer."""

from pathlib import Path

import pytest
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


@pytest.mark.parametrize("width", [390, 1280], ids=["phone", "desktop"])
def test_direct_feedback_entry_keeps_reading_position(
    page: Page,
    scoring_session: tuple[str, str],
    width: int,
) -> None:
    """Review expansion, autosave and a keyboard-sized resize must not follow the bottom."""
    base_url, session_id = scoring_session
    seed_committed_turn(
        session_id,
        prompt="Review the completed result.",
        reply="Completed fixture result.\n\n"
        + "\n\n".join(f"Verification detail {index}." for index in range(14)),
    )
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    partial = page.get_by_role("button", name="Partial", exact=True)
    expect(partial).to_be_enabled(timeout=30_000)
    partial.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    before_selection = partial.bounding_box()
    assert before_selection is not None

    partial.click()
    comment = page.get_by_role("textbox", name="Task review comment")
    expect(comment).to_be_visible()
    decision = page.locator("details").filter(has_text="Show model decision")
    expect(decision).to_have_attribute("open", "")
    expect(page.get_by_role("button", name="Edit feedback comment")).to_have_count(0)
    page.wait_for_timeout(300)
    after_selection = partial.bounding_box()
    assert after_selection is not None
    assert abs(after_selection["y"] - before_selection["y"]) <= 2, (
        before_selection,
        after_selection,
    )

    comment.fill("Could not find the deployment; please include the direct link.")
    before_save = comment.bounding_box()
    assert before_save is not None
    expect(page.get_by_label("Feedback save status")).to_have_text("Saved", timeout=10_000)
    page.wait_for_timeout(300)
    after_save = comment.bounding_box()
    assert after_save is not None
    assert abs(after_save["y"] - before_save["y"]) <= 2, (before_save, after_save)
    expect(comment).to_be_focused()

    # A mobile keyboard shrinks the usable viewport while this same field is
    # focused. Do not let the composer resize handler drag the reader away.
    page.set_viewport_size({"width": width, "height": 644})
    page.wait_for_timeout(300)
    after_resize = comment.bounding_box()
    assert after_resize is not None
    assert abs(after_resize["y"] - after_save["y"]) <= 2, (after_save, after_resize)
    expect(comment).to_be_focused()

    page.reload()
    expect(comment).to_have_value(
        "Could not find the deployment; please include the direct link.", timeout=30_000
    )
    expect(decision).to_have_attribute("open", "")
