"""Scoring eligibility is independent of outcome, human notes, and retention."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from omnigent.util.test_session_policy import (
    TEST_RETENTION_LABEL,
    TEST_RUN_LABEL,
    session_score_eligible,
)

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore

ExclusionReason = Literal["test_fixture", "duplicate", "out_of_scope", "other"]
EXCLUSION_REASONS = frozenset({"test_fixture", "duplicate", "out_of_scope", "other"})


def latest_scoring_eligibility(
    events: Sequence[dict], *, actor: str, conversation_id: str
) -> dict[str, dict]:
    """Fold append-order revisions, caller/conversation scoped (not timestamps)."""
    latest: dict[str, dict] = {}
    for row in events:
        if (
            row.get("kind") == "scoring_eligibility"
            and row.get("created_by") == actor
            and row.get("conversation_id") == conversation_id
            and isinstance(row.get("response_id"), str)
        ):
            latest[row["response_id"]] = row
    return latest


def select_scored_outcomes(
    events: Sequence[dict],
    *,
    actor: str,
    conversation_id: str,
    labels: Mapping[str, str],
) -> list[dict]:
    """Project latest human outcomes, THEN exclude. Never count revision history.

    This is a numerical/export projection, NOT an AI prompt. Unrated and Not sure
    stay missing, never zero. Raw outcome events remain available for audit.
    """
    if not session_score_eligible(labels):
        return []
    eligibility = latest_scoring_eligibility(events, actor=actor, conversation_id=conversation_id)
    outcomes: dict[str, dict] = {}
    for row in events:
        if (
            row.get("kind") == "outcome"
            and row.get("created_by") == actor
            and row.get("conversation_id") == conversation_id
            and row.get("review_source", "human") == "human"
            and isinstance(row.get("response_id"), str)
        ):
            outcomes[row["response_id"]] = row
    result = []
    for response_id, row in outcomes.items():
        # Missing eligibility is a legacy included record; malformed values do
        # not silently re-include an explicitly managed observation.
        if eligibility.get(response_id, {}).get("score_eligible", True) is not True:
            continue
        outcome = row.get("outcome")
        if outcome not in ("success", "partial", "failed"):
            continue
        result.append(
            {
                "conversation_id": conversation_id,
                "response_id": response_id,
                "outcome": outcome,
                "first_attempt_success": int(outcome == "success"),
            }
        )
    return result


def build_blind_scoring_input(record: Mapping[str, object]) -> dict[str, object]:
    """Allowlist v1 model input; never serialize whole sessions or review events.

    Callers extract task/answer from the original task transcript. Human tags,
    comments, outcomes, eligibility, titles, labels, and test/retention metadata
    are deliberately absent. Adding machine evidence needs a reviewed schema
    extension, not a generic metadata/context passthrough.
    """
    task, answer = record.get("task"), record.get("answer")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Scoring requires the original task text")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Scoring requires the completed answer text")
    return {"schema_version": 1, "task": task, "answer": answer}


def scoring_policy(store: ConversationStore, conversation_id: str, actor: str) -> dict:
    from omnigent.server.task_experiment import list_experiment_events

    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise ValueError("Session no longer exists")
    labels = conversation.labels or {}
    events = list_experiment_events(store, conversation_id)
    latest = latest_scoring_eligibility(events, actor=actor, conversation_id=conversation_id)
    return {
        "score_eligible": session_score_eligible(labels),
        "is_test": TEST_RUN_LABEL in labels,
        "retention": labels.get(TEST_RETENTION_LABEL),
        "responses": {
            response_id: {
                "score_eligible": row.get("score_eligible") is True,
                "exclusion_reason": row.get("exclusion_reason"),
            }
            for response_id, row in latest.items()
        },
    }


def save_scoring_eligibility(
    store: ConversationStore,
    conversation_id: str,
    response_id: str,
    actor: str,
    score_eligible: bool,
    exclusion_reason: ExclusionReason | None = None,
) -> dict:
    from omnigent.entities.conversation import ResourceEventData
    from omnigent.server.task_experiment import experiment_item, require_completed_answer

    if type(score_eligible) is not bool:
        raise ValueError("score_eligible must be a boolean")
    if exclusion_reason is not None and exclusion_reason not in EXCLUSION_REASONS:
        raise ValueError("Unknown exclusion reason")
    if score_eligible and exclusion_reason is not None:
        raise ValueError("Included responses cannot have an exclusion reason")
    require_completed_answer(store, conversation_id, response_id)
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise ValueError("Session no longer exists")
    if score_eligible and not session_score_eligible(conversation.labels or {}):
        raise ValueError("Session policy excludes all responses from scoring")
    item = store.append(
        conversation_id,
        [
            experiment_item(
                conversation_id=conversation_id,
                attempt_id=response_id,
                response_id=response_id,
                kind="scoring_eligibility",
                actor=actor,
                payload={
                    "score_eligible": score_eligible,
                    "exclusion_reason": exclusion_reason,
                    "review_source": "human",
                },
            )
        ],
    )[0]
    assert isinstance(item.data, ResourceEventData)
    return {
        **(item.data.resource or {}),
        "id": item.id,
        "response_id": response_id,
        "created_at": item.created_at,
        "created_by": actor,
    }
