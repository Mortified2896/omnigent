"""Mobile exclusion is independent of the outcome and human review tags."""

from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn


def test_unrated_exclusion_survives_review_and_reload(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    seed_committed_turn(session_id, prompt="Disposable example task", reply="Example answer")
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    exclude = page.get_by_role("button", name="Do not score", exact=True)
    expect(exclude).to_be_enabled(timeout=30_000)
    exclude.click()
    reason = page.get_by_label("Scoring exclusion reason")
    expect(reason).to_be_enabled()
    with page.expect_response(
        lambda response: "/scoring-eligibility/" in response.url
        and response.request.method == "PUT" and response.status == 200
    ):
        reason.select_option("test_fixture")
    # Wait for the mutation and policy read-back, not merely select.value.
    expect(reason).to_be_enabled()
    page.reload()
    expect(exclude).to_have_attribute("aria-pressed", "true", timeout=30_000)
    expect(reason).to_have_value("test_fixture")
    for label in ("Success", "Partial", "Failed", "Not sure"):
        expect(page.get_by_role("button", name=label, exact=True)).to_have_attribute(
            "aria-pressed", "false"
        )
    page.get_by_role("button", name="Success", exact=True).click()
    comment = page.get_by_label("Task review comment")
    expect(comment).to_be_visible()
    comment.fill("Keep this human-only note")
    tag = page.get_by_role("button", name="Tests/verification", exact=True)
    tag.click()
    save = page.get_by_role("button", name="Save details", exact=True)
    with page.expect_response(
        lambda response: "/task-outcomes/" in response.url
        and response.request.method == "PUT" and response.status == 200
    ):
        save.click()
    expect(save).to_be_disabled()
    # The remounted editor has no unsaved draft after read-back.
    expect(comment).to_have_value("Keep this human-only note")
    page.reload()
    expect(comment).to_have_value("Keep this human-only note", timeout=30_000)
    expect(tag).to_have_attribute("aria-pressed", "true")
    expect(exclude).to_have_attribute("aria-pressed", "true")
    exclude.click()
    expect(exclude).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_role("button", name="Success", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(comment).to_have_value("Keep this human-only note")
    page.screenshot(path=str(tmp_path / "scoring-eligibility-mobile.png"), full_page=True)
