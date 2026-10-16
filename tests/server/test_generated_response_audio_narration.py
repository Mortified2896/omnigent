"""Regression coverage for nullable item pages and exact-response narration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.server.generated_response_audio import (
    _response_markdown,
    _response_narration,
    _response_narration_for_id,
)


@pytest.mark.parametrize("page", [None, [], SimpleNamespace(data=None), SimpleNamespace(data=[])])
def test_latest_narration_accepts_an_empty_item_page(page: object) -> None:
    store = SimpleNamespace(list_items=Mock(return_value=page))
    assert _response_narration(store, "conversation") == (None, "")
    store.list_items.assert_called_once_with(
        conversation_id="conversation", limit=1000, order="desc"
    )


@pytest.mark.parametrize("page", [None, [], SimpleNamespace(data=None), SimpleNamespace(data=[])])
def test_exact_narration_accepts_an_empty_item_page(page: object) -> None:
    store = SimpleNamespace(list_items=Mock(return_value=page))
    assert _response_narration_for_id(store, "conversation", "response") == ""
    store.list_items.assert_called_once_with(
        conversation_id="conversation", limit=1000, order="desc"
    )


def _message(
    response_id: str,
    text: str,
    *,
    role: str = "assistant",
    status: str = "completed",
    is_meta: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        type="message",
        response_id=response_id,
        status=status,
        data=SimpleNamespace(
            role=role, is_meta=is_meta, content=[{"type": "output_text", "text": text}]
        ),
    )


def _work(response_id: str, item_type: str, **data: object) -> SimpleNamespace:
    return SimpleNamespace(
        type=item_type,
        response_id=response_id,
        status="completed",
        data=SimpleNamespace(**data),
    )


@pytest.mark.parametrize(
    "separator", ["function_call", "function_call_output", "reasoning", "native_tool"]
)
def test_report_narrates_only_the_final_answer_after_the_process_trace(separator: str) -> None:
    items = [
        _work("report", "resource_event"),
        _message("report", "Second finding"),
        _message("report", "# Daily briefing\nFirst finding"),
        _work("report", separator),
        _message("report", "The Mac server is reachable. I will check the reset next"),
        _work("report", "function_call"),
        _message("report", "I will research recent developments"),
    ]
    store = SimpleNamespace(list_items=Mock(return_value=SimpleNamespace(data=items)))
    assert _response_narration(store, "conversation") == (
        "report",
        "Next: Daily briefing. First finding. Second finding.",
    )
    assert _response_narration_for_id(store, "conversation", "report") == (
        "Next: Daily briefing. First finding. Second finding."
    )
    assert _response_markdown(items, "report") == "# Daily briefing\nFirst finding\nSecond finding"


def test_final_answer_looks_past_trailing_reasoning_and_turn_diff() -> None:
    items = [
        _work("report", "resource_event"),
        _work("report", "function_call_output", call_id="diff"),
        _work("report", "function_call", name="turn_diff", call_id="diff"),
        _work("report", "reasoning"),
        _message("report", "Final findings"),
        _message("report", "Hidden context", is_meta=True),
        _work("report", "function_call", name="web_search"),
        _message("report", "I am searching"),
    ]
    assert _response_markdown(items, "report") == "Final findings"


def test_tool_only_turn_does_not_narrate_its_progress_message() -> None:
    items = [
        _work("report", "function_call_output"),
        _work("report", "function_call", name="web_search"),
        _message("report", "I will search for updates"),
    ]
    store = SimpleNamespace(list_items=Mock(return_value=items))
    assert _response_narration(store, "conversation") == ("report", "")
    assert _response_narration_for_id(store, "conversation", "report") == ""


def test_latest_narration_preserves_order_and_ignores_non_answers() -> None:
    items = [
        _message("unfinished", "Still working", status="in_progress"),
        _message("metadata", "Hidden metadata", is_meta=True),
        _message("question", "User prompt", role="user"),
        _message("latest", "Second part"),
        _message("latest", "First part"),
        _message("older", "Earlier answer"),
    ]
    original = list(items)
    store = SimpleNamespace(list_items=Mock(return_value=SimpleNamespace(data=items)))
    assert _response_narration(store, "conversation") == ("latest", "First part. Second part.")
    assert items == original


def test_exact_narration_never_substitutes_a_newer_response() -> None:
    items = [_message("newer", "New answer"), _message("requested", "Original answer")]
    store = SimpleNamespace(list_items=Mock(return_value=items))
    assert _response_narration_for_id(store, "conversation", "requested") == "Original answer."
    assert _response_narration_for_id(store, "conversation", "missing") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("tts_url", [None, "http://127.0.0.1:9876", "http://localhost:9876"])
async def test_audio_startup_ignores_malformed_proxy_for_local_backend(
    monkeypatch: pytest.MonkeyPatch, tts_url: str | None
) -> None:
    from omnigent.server.generated_response_audio import GeneratedResponseAudioCoordinator

    monkeypatch.setenv("NO_PROXY", "fe80::/10")
    coordinator = GeneratedResponseAudioCoordinator(
        audio_store=SimpleNamespace(
            recover_processing=Mock(), list_pending_all_workspaces=Mock(return_value=[])
        ),
        conversation_store=Mock(),
        artifact_store=Mock(),
        tts_url=tts_url,
    )
    try:
        await coordinator.start()
        assert coordinator._client is not None
        assert coordinator._worker is not None
    finally:
        await coordinator.stop()
