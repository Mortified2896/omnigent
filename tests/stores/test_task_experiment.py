"""Same-schema experiment events preserve labels, revisions and subjective feedback."""

import pytest
from pydantic import ValidationError

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.task_experiment import (
    HumanForecast,
    experiment_item,
    first_attempt_success,
    list_experiment_events,
    save_outcome,
)
from omnigent.stores.conversation_store import InvalidFeedbackTargetError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize(
    "label,expected", [("success", 1), ("partial", 0), ("failed", 0), ("not_sure", None)]
)
def test_outcome_semantics(label, expected):
    assert first_attempt_success(label) == expected


@pytest.mark.parametrize("probability", [-1, 101, float("nan"), float("inf")])
def test_invalid_forecast(probability):
    with pytest.raises(ValidationError):
        HumanForecast(probability=probability)


def test_null_is_not_zero():
    assert HumanForecast().probability is None
    assert HumanForecast(probability=0).probability == 0


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
        kind="forecast",
        payload={"human_probability": None},
        actor="alice",
        idempotency_key="forecast:attempt",
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
    assert rows[0]["human_probability"] is None


def test_forecast_immutable_exact_configuration(conversation_store):
    from omnigent.server.schemas import SessionEventInput
    from omnigent.server.task_experiment import commit_forecast

    store = conversation_store
    conv = store.create_conversation()
    conv.model_override = "codex/example-model"
    conv.reasoning_effort = "high"
    body = SessionEventInput(
        type="message",
        success_forecast={"probability": 78},
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "test task"}],
            "stable_id": "a" * 32,
        },
    )
    attempt = commit_forecast(store, conv, body, "alice", "codex-native")
    assert commit_forecast(store, conv, body, "alice", "codex-native") == attempt
    rows = list_experiment_events(store, conv.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["selected_model"] == "codex/example-model"
    assert row["canonical_model"] == "example-model"
    assert row["selected_reasoning_effort"] == "high"
    assert row["selected_harness"] == "codex-native"
    assert row["human_probability"] == 78
    assert "test task" not in str(row)
    changed = body.model_copy(update={"success_forecast": HumanForecast(probability=90)})
    with pytest.raises(ValueError, match="immutable"):
        commit_forecast(store, conv, changed, "alice", "codex-native")
    assert list_experiment_events(store, conv.id)[0]["human_probability"] == 78
