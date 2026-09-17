"""Versioned, append-only experiment records in existing resource-event storage."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities.conversation import NewConversationItem, ResourceEventData
from omnigent.stores.conversation_store import ConversationStore

RESOURCE_TYPE = "task-success-experiment"
Outcome = Literal["success", "partial", "failed", "not_sure"]


class HumanForecast(BaseModel):
    """A pre-execution estimate; absence is distinct from zero percent."""

    model_config = ConfigDict(extra="forbid")
    probability: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False, strict=True)
    exposure: Literal["independent", "after-recommendation"] = "independent"
    source: Literal["normal-composer", "synthetic-acceptance"] = "normal-composer"


def first_attempt_success(outcome: Outcome) -> int | None:
    return None if outcome == "not_sure" else int(outcome == "success")


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


def content_digest(content: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def save_outcome(
    store: ConversationStore, conversation_id: str, response_id: str, actor: str, outcome: Outcome
) -> dict:
    """Append a revision independently of subjective response feedback."""
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


def commit_forecast(
    store: ConversationStore, conversation, body, actor: str | None, harness: str | None = None
) -> str | None:
    """Commit before dispatch; the original record wins on transport retries."""
    if body.success_forecast is None:
        return None
    if body.type not in ("message", "slash_command") or (
        body.type == "message" and body.data.get("role") != "user"
    ):
        raise ValueError("Success forecasts require a user message")
    stable_id = body.data.get("stable_id")
    if not isinstance(stable_id, str) or re.fullmatch(r"[0-9a-f]{32}", stable_id) is None:
        raise ValueError("Success forecasts require a stable input identity")
    from omnigent.server.auth import RESERVED_USER_LOCAL

    actor = actor or RESERVED_USER_LOCAL
    attempt_id = "attempt_" + stable_id
    model = body.model_override if body.model_override is not None else conversation.model_override
    payload = {
        "human_probability": body.success_forecast.probability,
        "human_exposure": body.success_forecast.exposure,
        "input_stable_id": stable_id,
        "input_digest": content_digest(
            body.data.get(
                "content",
                [
                    {
                        "type": "skill",
                        "name": body.data.get("name"),
                        "arguments": body.data.get("arguments"),
                    }
                ],
            )
        ),
        "selected_harness": harness or conversation.harness_override,
        "selected_model": model,
        "canonical_model": model.removeprefix("codex/") if model else None,
        "selected_reasoning_effort": conversation.reasoning_effort,
        "selection_mode": "manual-human-choice",
        "o3_forecast": None,
        "forecaster_id": None,
        "candidate_set": None,
        "policy_version": None,
        "selection_propensity": None,
        "experiment_source": body.success_forecast.source,
        "controlled_exploration": None,
    }
    item = experiment_item(
        conversation_id=conversation.id,
        attempt_id=attempt_id,
        kind="forecast",
        payload=payload,
        actor=actor,
        idempotency_key=f"forecast:{attempt_id}",
    )
    saved = store.append(conversation.id, [item])[0]
    if saved.data != item.data or saved.created_by != actor:
        raise ValueError("The original forecast is immutable; start a new attempt")
    return attempt_id


def link_native_response(
    store: ConversationStore,
    conversation_id: str,
    attempt_id: str,
    response_id: str,
    actor: str | None,
) -> None:
    from omnigent.server.auth import RESERVED_USER_LOCAL

    actor = actor or RESERVED_USER_LOCAL
    store.append(
        conversation_id,
        [
            experiment_item(
                conversation_id=conversation_id,
                attempt_id=attempt_id,
                kind="response_link",
                response_id=response_id,
                actor=actor,
                payload={"native_response_id": response_id},
                idempotency_key=f"link:{attempt_id}",
            )
        ],
    )


def accept_response_link(store: ConversationStore, conversation_id: str, event: dict) -> None:
    """Bind only an existing forecast from the same conversation, using runner evidence."""
    attempt_id = event.get("attempt_id")
    response_id = event.get("native_response_id")
    if not isinstance(attempt_id, str) or not isinstance(response_id, str) or not response_id:
        return
    rows = list_experiment_events(store, conversation_id)
    forecast = next(
        (row for row in rows if row["kind"] == "forecast" and row["attempt_id"] == attempt_id),
        None,
    )
    if forecast is None:
        return
    link_native_response(store, conversation_id, attempt_id, response_id, forecast["created_by"])
