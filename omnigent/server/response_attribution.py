"""Durable, response-scoped model and advisor attribution."""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from omnigent.entities.conversation import NewConversationItem, ResourceEventData
from omnigent.model_advisor_binding import decode_transport_binding_label
from omnigent.stores.conversation_store import (
    ADVISOR_DISPATCH_ROUTE_LABEL_KEY,
    ADVISOR_TRANSPORT_PLAN_LABEL_KEY,
    ConversationStore,
)

RESOURCE_TYPE = "response-execution"
ModelSource = Literal["response_usage", "session_reported", "unknown"]


def response_attribution_item(
    *,
    conversation_id: str,
    response_id: str,
    requested_model: str | None,
    actual_model: str | None,
    model_source: ModelSource,
    reasoning_effort: str | None = None,
    access_lane: str | None = None,
    advisor_round_id: str | None = None,
) -> NewConversationItem:
    """Build an idempotent event linked to the exact assistant response."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "conversation_id": conversation_id,
        "response_id": response_id,
        "requested_model": requested_model,
        "actual_model": actual_model,
        "model_status": "observed" if actual_model else "unknown",
        "model_source": model_source,
        "reasoning_effort": reasoning_effort,
        "access_lane": access_lane,
        "advisor_round_id": advisor_round_id,
    }
    return NewConversationItem(
        type="resource_event",
        response_id=response_id,
        stable_id=uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"response-execution:{conversation_id}:{response_id}",
        ).hex,
        data=ResourceEventData(
            event_type="response.execution.completed",
            resource_id=response_id,
            resource_type=RESOURCE_TYPE,
            resource=payload,
        ),
    )


def list_response_attributions(
    store: ConversationStore,
    conversation_id: str,
) -> dict[str, dict[str, Any]]:
    """Return response execution records without mixing them into outcomes."""
    result: dict[str, dict[str, Any]] = {}
    after = None
    while True:
        page = store.list_items(
            conversation_id,
            type="resource_event",
            limit=100,
            after=after,
        )
        for item in page.data:
            data = item.data
            if not isinstance(data, ResourceEventData) or data.resource_type != RESOURCE_TYPE:
                continue
            payload = data.resource or {}
            response_id = payload.get("response_id")
            if (
                payload.get("conversation_id") != conversation_id
                or not isinstance(response_id, str)
                or response_id != item.response_id
            ):
                continue
            result[response_id] = {
                key: payload.get(key)
                for key in (
                    "requested_model",
                    "actual_model",
                    "model_status",
                    "model_source",
                    "reasoning_effort",
                    "access_lane",
                    "advisor_round_id",
                )
            }
        if not page.has_more:
            return result
        after = page.data[-1].id


def response_route_from_labels(labels: dict[str, str]) -> str | None:
    """Summarize the concrete transport without revealing the selected model."""
    lane = labels.get("omnigent.access_lane")
    if lane == "omniroute":
        return "omniroute"
    if lane not in {"codex-direct", "glm-direct"}:
        return None
    try:
        plan_raw = decode_transport_binding_label(labels, ADVISOR_TRANSPORT_PLAN_LABEL_KEY)
        route_raw = decode_transport_binding_label(labels, ADVISOR_DISPATCH_ROUTE_LABEL_KEY)
        plan = json.loads(plan_raw) if isinstance(plan_raw, str) else None
        route = json.loads(route_raw) if isinstance(route_raw, str) else None
        if (
            isinstance(plan, dict)
            and isinstance(route, dict)
            and isinstance(plan.get("primary"), dict)
            and plan["primary"].get("transport") == "omniroute"
            and route.get("transport") == "direct"
            and route == plan.get("fallback")
        ):
            return "direct_fallback"
    except ValueError:
        pass
    return "direct"


def list_response_routes(store: ConversationStore, conversation_id: str) -> dict[str, str | None]:
    """Read per-response snapshots; never infer historical routes from current settings."""
    result: dict[str, str | None] = {}
    after = None
    while True:
        page = store.list_items(conversation_id, type="resource_event", limit=100, after=after)
        for item in page.data:
            data = item.data
            if not isinstance(data, ResourceEventData):
                continue
            payload = data.resource or {}
            response_id = payload.get("response_id")
            if (
                payload.get("conversation_id") != conversation_id
                or response_id != item.response_id
            ):
                continue
            if not isinstance(response_id, str):
                continue
            if data.resource_type == "response-advisor-binding" and "route" in payload:
                result[response_id] = payload["route"]
            elif data.resource_type == RESOURCE_TYPE and response_id not in result:
                # Legacy records establish the lane, but not whether Direct was a fallback.
                result[response_id] = response_route_from_labels(
                    {"omnigent.access_lane": payload.get("access_lane")}
                )
        if not page.has_more:
            return result
        after = page.data[-1].id


def bind_response_advisor_round(
    store: ConversationStore,
    conversation_id: str,
    response_id: str,
    advisor_round_id: str | None,
) -> str | None:
    """Consume a confirmed decision once, including turns that later fail.

    Session labels retain route provenance for authorization and transport;
    they are not evidence that the advisor reviewed every later message.
    Persist the binding at response start so a failed turn cannot leak its
    decision into the next ordinary message. Replayed lifecycle events keep
    their original binding, including an explicit no-advisor binding.
    """
    binding_type = "response-advisor-binding"
    used_rounds: set[str] = set()
    after = None
    while True:
        page = store.list_items(conversation_id, type="resource_event", limit=100, after=after)
        for item in page.data:
            data = item.data
            if not isinstance(data, ResourceEventData):
                continue
            if data.resource_type not in {binding_type, RESOURCE_TYPE}:
                continue
            payload = data.resource or {}
            if payload.get("conversation_id") != conversation_id:
                continue
            bound_response = payload.get("response_id")
            if not isinstance(bound_response, str) or bound_response != item.response_id:
                continue
            bound_round = payload.get("advisor_round_id")
            if bound_response == response_id:
                return bound_round if isinstance(bound_round, str) else None
            if isinstance(bound_round, str):
                used_rounds.add(bound_round)
        if not page.has_more:
            break
        after = page.data[-1].id
    selected = (
        advisor_round_id if advisor_round_id and advisor_round_id not in used_rounds else None
    )
    conversation = store.get_conversation(conversation_id)
    route = response_route_from_labels((conversation.labels or {}) if conversation else {})
    store.append(
        conversation_id,
        [
            NewConversationItem(
                type="resource_event",
                response_id=response_id,
                stable_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"response-advisor-binding:{conversation_id}:{response_id}"
                ).hex,
                data=ResourceEventData(
                    event_type="response.advisor.bound",
                    resource_id=response_id,
                    resource_type=binding_type,
                    resource={
                        "conversation_id": conversation_id,
                        "response_id": response_id,
                        "advisor_round_id": selected,
                        "route": route,
                    },
                ),
            )
        ],
    )
    return selected
