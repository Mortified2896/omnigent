"""Append-only human task-outcome records in existing resource-event storage."""

from __future__ import annotations

import uuid
from typing import Literal

from omnigent.entities.conversation import NewConversationItem, ResourceEventData
from omnigent.stores.conversation_store import ConversationStore

RESOURCE_TYPE = "task-success-experiment"
Outcome = Literal["success", "partial", "failed", "not_sure"]
MAX_COMMENT_LENGTH = 4000
MAX_TAGS = 8
MAX_TAG_LENGTH = 64


def first_attempt_success(outcome: Outcome) -> int | None:
    return None if outcome == "not_sure" else int(outcome == "success")


def normalize_tags(tags: list[str] | None) -> list[str]:
    """Normalize small human/model tag sets while preserving display spelling."""
    if tags is None:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in tags:
        tag = " ".join(value.split()).strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"Task outcome tags may be at most {MAX_TAG_LENGTH} characters")
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) > MAX_TAGS:
            raise ValueError(f"Task outcomes may contain at most {MAX_TAGS} tags")
    return result


def experiment_item(
    *,
    conversation_id: str,
    attempt_id: str,
    kind: str,
    payload: dict,
    actor: str | None,
    response_id: str | None = None,
    idempotency_key: str | None = None,
) -> NewConversationItem:
    """Use the existing extensible event envelope so old releases can read it."""
    return NewConversationItem(
        type="resource_event",
        response_id=response_id or attempt_id,
        created_by=actor,
        stable_id=(
            uuid.uuid5(uuid.NAMESPACE_URL, f"experiment:{conversation_id}:{idempotency_key}").hex
            if idempotency_key
            else None
        ),
        data=ResourceEventData(
            event_type=f"task.experiment.{kind}",
            resource_id=attempt_id,
            resource_type=RESOURCE_TYPE,
            resource={
                "schema_version": 1,
                "conversation_id": conversation_id,
                "attempt_id": attempt_id,
                "kind": kind,
                **payload,
            },
        ),
    )


def list_experiment_events(store: ConversationStore, conversation_id: str) -> list[dict]:
    """Read all pages using the existing conversation/type/position index."""
    result = []
    after = None
    while True:
        page = store.list_items(conversation_id, type="resource_event", limit=100, after=after)
        for item in page.data:
            data = item.data
            if isinstance(data, ResourceEventData) and data.resource_type == RESOURCE_TYPE:
                payload = data.resource or {}
                # Forks retain provenance but must not acquire their source's observations.
                if payload.get("conversation_id") == conversation_id:
                    result.append(
                        {
                            **payload,
                            "id": item.id,
                            "response_id": item.response_id,
                            "created_at": item.created_at,
                            "created_by": item.created_by,
                        }
                    )
        if not page.has_more:
            return result
        after = page.data[-1].id


def require_completed_answer(
    store: ConversationStore, conversation_id: str, response_id: str
) -> None:
    """Require the same durable answer target for outcome and eligibility edits."""
    from omnigent.entities.conversation import MessageData
    from omnigent.stores.conversation_store import InvalidFeedbackTargetError

    after = None
    eligible = False
    while not eligible:
        page = store.list_items(conversation_id, type="message", limit=100, after=after)
        eligible = any(
            item.response_id == response_id
            and item.status == "completed"
            and isinstance(item.data, MessageData)
            and item.data.role == "assistant"
            and not item.data.is_meta
            and not item.data.interrupted
            for item in page.data
        )
        if eligible or not page.has_more:
            break
        after = page.data[-1].id
    if not eligible:
        raise InvalidFeedbackTargetError(
            "Response is not a completed assistant answer in this session"
        )



def save_outcome(
    store: ConversationStore,
    conversation_id: str,
    response_id: str,
    actor: str,
    outcome: Outcome,
    *,
    comment: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Append a complete human-review revision independently of subjective feedback."""
    if comment is not None and len(comment) > MAX_COMMENT_LENGTH:
        raise ValueError(f"Task outcome comments may be at most {MAX_COMMENT_LENGTH} characters")
    normalized_comment = comment.strip() if isinstance(comment, str) else None
    normalized_tags = normalize_tags(tags)

    require_completed_answer(store, conversation_id, response_id)
    links = [
        row
        for row in list_experiment_events(store, conversation_id)
        if row["kind"] == "response_link"
        and row["response_id"] == response_id
        and row["created_by"] == actor
    ]
    attempt_id = links[-1]["attempt_id"] if links else response_id
    item = store.append(
        conversation_id,
        [
            experiment_item(
                conversation_id=conversation_id,
                attempt_id=attempt_id,
                response_id=response_id,
                kind="outcome",
                actor=actor,
                payload={
                    "outcome": outcome,
                    "first_attempt_success": first_attempt_success(outcome),
                    "comment": normalized_comment,
                    "tags": normalized_tags,
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
