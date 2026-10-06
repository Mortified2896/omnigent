"""Explicit human-requested feedback discussions, separate from blind scoring."""

from __future__ import annotations

import json
from typing import Any

from omnigent.entities.conversation import ResourceEventData
from omnigent.server.response_attribution import list_response_attributions
from omnigent.server.task_experiment import list_experiment_events, require_completed_answer
from omnigent.stores.conversation_store import ConversationStore

PREFIX = "omnigent.feedback."
CONTEXT_RESOURCE_TYPE = "feedback-discussion-context"


def discussion_snapshot(store: ConversationStore, session_id: str) -> dict[str, Any] | None:
    """Read the immutable original from the discussion, outside bounded labels."""
    after = None
    while True:
        page = store.list_items(session_id, type="resource_event", limit=100, after=after)
        for item in page.data:
            data = item.data
            if isinstance(data, ResourceEventData) and data.resource_type == CONTEXT_RESOURCE_TYPE:
                return data.resource
        if not page.has_more:
            return None
        after = page.data[-1].id


def original_feedback(
    store: ConversationStore, session_id: str, response_id: str, actor: str
) -> dict[str, Any]:
    require_completed_answer(store, session_id, response_id)
    rows = [
        row
        for row in list_experiment_events(store, session_id)
        if row["response_id"] == response_id
        and row["created_by"] == actor
        and row["kind"] == "outcome"
        and row.get("outcome")
    ]
    if not rows:
        raise ValueError("Save an outcome before asking for AI perspective")
    row = rows[-1]
    attribution = list_response_attributions(store, session_id).get(response_id) or {}
    return {
        "outcome": row["outcome"],
        "comment": row.get("comment") or "",
        "tags": row.get("tags") or [],
        "revision_id": row["id"],
        "model": attribution.get("actual_model") or attribution.get("requested_model"),
        "reasoning_effort": attribution.get("reasoning_effort"),
        "access_lane": attribution.get("access_lane"),
    }


def discussion_instructions(snapshot: dict[str, Any]) -> str:
    return (
        "This is an explicitly requested discussion of the human's feedback on your last answer. "
        "Their outcome rating is authoritative. Explain your technical perspective, including "
        "uncertainties, and discuss corrections with them. Do not execute tools or change project "
        "files for this review. Never change the outcome. "
        "When proposing revised feedback, include "
        'one fenced feedback-json block containing {"comment": "...", "tags": ["..."]}. '
        "Use at most 4000 characters in comment and 8 tags of at most 64 characters each. "
        "The human must accept changes before they affect the saved feedback. "
        "Original feedback (immutable discussion snapshot):\n"
        + json.dumps(snapshot, ensure_ascii=False)
    )
