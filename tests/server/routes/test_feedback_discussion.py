import pytest

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.feedback_discussion import discussion_instructions, original_feedback
from omnigent.server.response_attribution import response_attribution_item
from omnigent.server.task_experiment import save_outcome
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def test_discussion_keeps_original_human_revision_and_responding_model(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
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
    store.append(
        conv.id,
        [
            response_attribution_item(
                conversation_id=conv.id,
                response_id="answer",
                requested_model="gpt-6.1-sol",
                actual_model="gpt-6-astra",
                model_source="response_usage",
                reasoning_effort="high",
                access_lane="codex-direct",
            )
        ],
    )
    save_outcome(
        store,
        conv.id,
        "answer",
        "alice",
        "partial",
        comment="Instructions issue",
        tags=["Instructions"],
    )
    save_outcome(store, conv.id, "answer", "bob", "success", comment="Bob's private feedback")
    snapshot = original_feedback(store, conv.id, "answer", "alice")
    assert snapshot["model"] == "gpt-6-astra"
    assert snapshot["reasoning_effort"] == "high"
    assert snapshot["comment"] == "Instructions issue"
    save_outcome(
        store,
        conv.id,
        "answer",
        "alice",
        "partial",
        comment="Actually an environment issue",
        tags=["Environment"],
    )
    assert snapshot["tags"] == ["Instructions"]
    assert original_feedback(store, conv.id, "answer", "alice")["tags"] == ["Environment"]
    instructions = discussion_instructions(snapshot)
    assert "feedback-json" in instructions and "Never change the outcome" in instructions
    assert "Bob's private feedback" not in instructions
    with pytest.raises(ValueError, match="Save an outcome"):
        original_feedback(store, conv.id, "answer", "charlie")
