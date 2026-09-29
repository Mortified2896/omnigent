"""Same-schema experiment events preserve labels, revisions and subjective feedback."""

import pytest

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.task_experiment import (
    experiment_item,
    first_attempt_success,
    list_experiment_events,
    normalize_tags,
    save_outcome,
)
from omnigent.server.response_attribution import (
    list_response_attributions,
    response_attribution_item,
)
from omnigent.stores.conversation_store import InvalidFeedbackTargetError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize(
    "label,expected", [("success", 1), ("partial", 0), ("failed", 0), ("not_sure", None)]
)
def test_outcome_semantics(label, expected):
    assert first_attempt_success(label) == expected


def test_tag_normalization_and_limits():
    assert normalize_tags(["  A  ", "a", "b  c", "", "b c", "d"]) == ["A", "b c", "d"]
    with pytest.raises(ValueError):
        normalize_tags(["x" * 65])
    with pytest.raises(ValueError):
        normalize_tags([f"t{i}" for i in range(9)])
    assert normalize_tags(None) == []


def test_revisions_and_feedback_independent(conversation_store):
    store = conversation_store
    conv = store.create_conversation()
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="answer",
                data=MessageData(
                    role="assistant",
                    agent="test",
                    content=[{"type": "output_text", "text": "Done"}],
                ),
            )
        ],
    )
    store.put_response_feedback(
        conv.id, "answer", "alice", -1, comment="Unpleasant", update_comment=True
    )
    for label in ("not_sure", "partial", "failed", "success"):
        save_outcome(store, conv.id, "answer", "alice", label)
    reopened = SqlAlchemyConversationStore(store.storage_location)
    rows = list_experiment_events(reopened, conv.id)
    assert [r["outcome"] for r in rows] == ["not_sure", "partial", "failed", "success"]
    assert [r["first_attempt_success"] for r in rows] == [None, 0, 0, 1]
    assert all(r["created_by"] == "alice" for r in rows)
    assert len({r["id"] for r in rows}) == 4
    assert reopened.list_response_feedback(conv.id, "alice")[0].rating == -1
    store.delete_response_feedback(conv.id, "answer", "alice")
    assert len(list_experiment_events(store, conv.id)) == 4
    with pytest.raises(InvalidFeedbackTargetError):
        save_outcome(store, conv.id, "missing", "alice", "success")


def test_event_pagination_and_idempotency(conversation_store):
    store = conversation_store
    conv = store.create_conversation()
    item = experiment_item(
        conversation_id=conv.id,
        attempt_id="attempt",
        kind="outcome",
        payload={"outcome": "not_sure"},
        actor="alice",
        idempotency_key="outcome:first",
    )
    first = store.append(conv.id, [item])[0]
    assert store.append(conv.id, [item])[0].id == first.id
    store.append(
        conv.id,
        [
            experiment_item(
                conversation_id=conv.id,
                attempt_id=str(i),
                kind="outcome",
                payload={"outcome": "not_sure"},
                actor="alice",
            )
            for i in range(120)
        ],
    )
    rows = list_experiment_events(store, conv.id)
    assert len(rows) == 121
    assert rows[0]["outcome"] == "not_sure"


def test_response_attribution_is_durable_and_attached_to_human_outcome(conversation_store):
    store = conversation_store
    conv = store.create_conversation()
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_123",
                data=MessageData(
                    role="assistant",
                    agent="test",
                    content=[{"type": "output_text", "text": "Done"}],
                ),
            ),
            response_attribution_item(
                conversation_id=conv.id,
                response_id="resp_123",
                requested_model="gpt-6-sol",
                actual_model="gpt-6-sol-2026-09-20",
                model_source="response_usage",
                reasoning_effort="high",
                access_lane="openai-direct",
                advisor_round_id="round_123",
            ),
        ],
    )

    reopened = SqlAlchemyConversationStore(store.storage_location)
    attribution = list_response_attributions(reopened, conv.id)["resp_123"]
    outcome = save_outcome(reopened, conv.id, "resp_123", "alice", "success")

    assert attribution == {
        "requested_model": "gpt-6-sol",
        "actual_model": "gpt-6-sol-2026-09-20",
        "model_status": "observed",
        "model_source": "response_usage",
        "reasoning_effort": "high",
        "access_lane": "openai-direct",
        "advisor_round_id": "round_123",
    }
    assert outcome["model_attribution"] == attribution
