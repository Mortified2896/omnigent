"""Regression coverage for nullable item pages and exact-response narration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.server.generated_response_audio import (
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
        response_id=response_id,
        status=status,
        data=SimpleNamespace(
            role=role, is_meta=is_meta, content=[{"type": "output_text", "text": text}]
        ),
    )


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
