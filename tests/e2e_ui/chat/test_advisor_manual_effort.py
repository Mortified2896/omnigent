"""Manual effort uses the real session PATCH after an Advisor-created launch."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.e2e_ui.conftest import _server_state, fetch_with_retry, seed_committed_turn
from tests.e2e_ui.start_session.test_model_advisor_composer import _CATALOG, _SAVED_PREFERENCES


@pytest.mark.parametrize("width", [390, 1280], ids=["phone", "desktop"])
def test_manual_max_persists_with_advisor_off(
    page: Page, scoring_session: tuple[str, str], width: int
) -> None:
    base_url, session_id = scoring_session
    seed_committed_turn(session_id, prompt="Retained fixture prompt.", reply="Fixture answer.")
    # Only the disposable, manifested fixture database is seeded. PATCHes and
    # reloads below use the real server, not a mocked persistence response.
    database_uri = _server_state.get("database_uri")
    if not database_uri:
        raise RuntimeError("This regression requires the isolated fixture database")
    store = SqlAlchemyConversationStore(str(database_uri))
    store.update_conversation(
        session_id,
        model_override="gpt-6.1-sol",
        reported_model="gpt-6.1-sol",
        reasoning_effort="high",
        labels={
            "omnigent.wrapper": "codex-native-ui",
            "omnigent.advisor.round_id": "round-original-high",
            "omnigent.advisor.logical_choice_id": "choice-original-high",
            "omnigent.advisor.connection_id": "codex-login",
            "omnigent.access_lane": "codex-direct",
        },
    )
    store.set_external_session_id(session_id, "fixture-native-thread")

    def native_catalog(route: Route) -> None:
        if (
            route.request.method != "GET"
            or urlparse(route.request.url).path != f"/v1/sessions/{session_id}"
        ):
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        # Deterministic harness discovery avoids launching a paid model. The
        # persisted choice, labels and effort are passed through unchanged.
        payload.update(harness="codex-native", host_id="fixture-host", workspace="/tmp")
        payload["model_options"] = [
            {
                "id": "gpt-6.1-sol",
                "model": "gpt-6.1-sol",
                "displayName": "GPT-6.1 Sol",
                "defaultReasoningEffort": "high",
                "isDefault": True,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": effort}
                    for effort in ("low", "medium", "high", "xhigh", "max")
                ],
            }
        ]
        route.fulfill(status=200, json=payload)

    advisor_posts: list[str] = []

    def advisor_reads(route: Route) -> None:
        if route.request.method != "GET":
            advisor_posts.append(route.request.url)
            route.abort()
            return
        route.fulfill(
            status=200,
            json=_CATALOG
            if urlparse(route.request.url).path.endswith("/catalog")
            else {
                "object": "model_advisor.preferences",
                "version": 1,
                "etag": '"fixture"',
                "state": "saved",
                "preferences": _SAVED_PREFERENCES,
                "logical_preferences": _SAVED_PREFERENCES,
            },
        )

    page.route("**/v1/sessions/**", native_catalog)
    page.route("**/v1/model-advisor/**", advisor_reads)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    imports = page.get_by_role("dialog", name="Your imports are ready")
    expect(imports).to_be_visible(timeout=15_000)
    imports.get_by_role("button", name="Close", exact=True).click()
    effort = page.get_by_test_id("composer-inline-effort")
    expect(effort).to_have_text("High", timeout=30_000)
    toggle = page.get_by_test_id("chat-model-advisor-toggle")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    draft = page.get_by_role("textbox", name="Message the agent")
    draft.fill("Keep this unsent follow-up while I choose Max.")

    effort.click()
    page.get_by_role("option", name="Max", exact=True).click()
    expect(effort).to_have_text("Max")
    expect(draft).to_have_value("Keep this unsent follow-up while I choose Max.")
    persisted = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).json()
    assert persisted["reasoning_effort"] == "max"
    assert persisted["labels"]["omnigent.advisor.manual_choice"] == "1"
    assert persisted["labels"]["omnigent.advisor.previous.round_id"] == "round-original-high"

    page.reload()
    expect(effort).to_have_text("Max", timeout=30_000)
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(draft).to_have_value("Keep this unsent follow-up while I choose Max.")
    assert advisor_posts == []
    expect(page.get_by_text("Model Advisor sessions are pinned", exact=False)).to_have_count(0)
