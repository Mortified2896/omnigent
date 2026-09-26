"""Durable response-to-audio association store tests."""

from __future__ import annotations

from omnigent.db.db_models import SqlGeneratedResponseAudio
from omnigent.db.utils import now_epoch
from omnigent.stores.generated_response_audio import (
    SqlAlchemyGeneratedResponseAudioStore,
)


def test_audio_state_is_idempotent_and_response_scoped(db_uri: str) -> None:
    store = SqlAlchemyGeneratedResponseAudioStore(db_uri)
    conversation_id = "a" * 32
    first = store.create_pending(conversation_id, "response-a", "daily-brief")
    duplicate = store.create_pending(conversation_id, "response-a", "daily-brief")
    other = store.create_pending(conversation_id, "response-b", "daily-brief")

    assert duplicate == first
    assert first.status == "pending"
    assert other.response_id == "response-b"
    assert store.claim_pending(conversation_id, "response-a") is True
    assert store.claim_pending(conversation_id, "response-a") is False
    assert (
        store.mark_ready(
            conversation_id,
            "response-a",
            artifact_key="generated-response-audio/test.wav",
            duration_seconds=3.25,
            sample_rate=24_000,
        )
        is True
    )

    ready = store.get(conversation_id, "response-a")
    assert ready is not None
    assert ready.status == "ready"
    assert ready.artifact_key == "generated-response-audio/test.wav"
    assert ready.duration_seconds == 3.25
    assert ready.sample_rate == 24_000
    assert [row.response_id for row in store.list_for_conversation(conversation_id)] == [
        "response-a",
        "response-b",
    ]


def test_recovery_leaves_recent_processing_job_alone(db_uri: str) -> None:
    store = SqlAlchemyGeneratedResponseAudioStore(db_uri)
    conversation_id = "b" * 32
    store.create_pending(conversation_id, "stale-response", "daily-brief")
    store.create_pending(conversation_id, "active-response", "daily-brief")
    assert store.claim_pending(conversation_id, "stale-response") is True
    assert store.claim_pending(conversation_id, "active-response") is True
    with store._session("backdate_stale_audio") as session:
        stale = session.get(
            SqlGeneratedResponseAudio,
            (0, conversation_id, "stale-response"),
        )
        assert stale is not None
        stale.updated_at = now_epoch() - 31 * 60

    assert store.recover_processing() == 1
    assert store.get(conversation_id, "stale-response").status == "pending"
    assert store.get(conversation_id, "active-response").status == "processing"
