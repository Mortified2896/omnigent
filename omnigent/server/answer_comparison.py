"""Paired, caller-owned feedback on an initial human/Jev disagreement.

Comparison events never change the session executor or count as task outcomes.
The durable claim prevents duplicate inference across tabs or server replicas.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from omnigent.answer_comparison import (
    CALL_TIMEOUT,
    MAX_ANSWER_CHARS,
    Vote,
    text_task_eligible,
)
from omnigent.entities.conversation import MessageData, NewConversationItem, ResourceEventData
from omnigent.model_advisor_provider_policy import plan_transport
from omnigent.model_advisor_provider_workflow import LogicalFrozenRound, LogicalReviewDecision
from omnigent.server.response_attribution import list_response_attributions
from omnigent.stores.conversation_store import ConversationStore

LABEL = "omnigent.advisor.answer_comparison"
RESOURCE = "jev-answer-comparison"


def round_eligible(frozen: LogicalFrozenRound, review: LogicalReviewDecision) -> bool:
    return (
        frozen.uses_jev_router
        and frozen.continuation_session_id is None
        and not frozen.attachments
        and review.human_choice_id != review.advisor_choice_id
        and text_task_eligible(frozen.task)
    )


def _items(store: ConversationStore, session_id: str):
    after = None
    while True:
        page = store.list_items(session_id, after=after, limit=100)
        yield from page.data
        if not page.has_more:
            return
        after = page.data[-1].id


def read_comparison(
    store: ConversationStore, session_id: str, response_id: str, actor: str
) -> dict[str, Any] | None:
    result: dict[str, Any] | None = None
    for item in _items(store, session_id):
        data = item.data
        if (
            item.response_id != response_id
            or item.created_by != actor
            or not isinstance(data, ResourceEventData)
            or data.resource_type != RESOURCE
        ):
            continue
        payload = data.resource or {}
        # Forks retain audit events, but never acquire their source's votes.
        if payload.get("session_id") != session_id:
            continue
        result = {**(result or {}), **payload, "revision": item.id}
    if result and result.get("state") == "pending":
        if time.time() - result.get("started_at", 0) > CALL_TIMEOUT + 30:
            result = {
                **result,
                "state": "failed",
                "error": "Comparison interrupted. The original answer is still available.",
            }
    return result


def append_event(
    store: ConversationStore,
    session_id: str,
    response_id: str,
    actor: str,
    payload: dict[str, Any],
    *,
    claim: bool = False,
):
    return store.append(
        session_id,
        [
            NewConversationItem(
                type="resource_event",
                response_id=response_id,
                created_by=actor,
                stable_id=(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"jev-answer-comparison:{session_id}:{response_id}:{actor}",
                    ).hex
                    if claim
                    else None
                ),
                data=ResourceEventData(
                    resource_type=RESOURCE,
                    resource_id=response_id,
                    event_type=f"jev.answer_comparison.{payload.get('state', 'vote')}",
                    resource={
                        "schema_version": 1,
                        "session_id": session_id,
                        "response_id": response_id,
                        **payload,
                    },
                ),
            )
        ],
    )[0]


def comparison_source(
    store: ConversationStore, repository: Any, session_id: str, response_id: str, actor: str
) -> dict[str, Any] | None:
    conv = store.get_conversation(session_id)
    if conv is None or (conv.labels or {}).get(LABEL) != "1" or not conv.host_id:
        return None
    if conv.live_status in {"running", "waiting", "launching"}:
        return None
    attribution = list_response_attributions(store, session_id).get(response_id) or {}
    round_id = attribution.get("advisor_round_id")
    if not isinstance(round_id, str):
        return None
    record = repository.load_round(actor, conv.host_id, round_id)
    if (
        record is None
        or record.payload.get("schema_version") != 2
        or record.payload.get("execution_session_id") != session_id
    ):
        return None
    frozen = LogicalFrozenRound.from_payload(record.payload["frozen"])
    review = LogicalReviewDecision.from_payload(record.payload["review"])
    if not round_eligible(frozen, review):
        return None
    choice = (record.payload.get("execution_plan") or {}).get("choice") or {}
    source_choice = next((c for c in frozen.pool if c.choice_id == _choice_id(choice)), None)
    if source_choice is None or source_choice.choice_id not in {
        review.human_choice_id,
        review.advisor_choice_id,
    }:
        return None

    # A changed or substituted execution is not evidence about the frozen pair.
    plan = record.payload.get("execution_plan") or {}
    wire_model = (plan.get("primary") or {}).get("wire_model")
    expected = {source_choice.model_id.lower()}
    if isinstance(wire_model, str):
        expected.add(wire_model.lower())
    for key in ("requested_model", "actual_model"):
        observed = attribution.get(key)
        if isinstance(observed, str) and observed.lower() not in expected:
            return None
    effort = attribution.get("reasoning_effort")
    if effort is not None and effort != source_choice.reasoning_effort:
        return None

    answer_parts: list[str] = []
    human_messages = 0
    seen_answer = False
    for item in _items(store, session_id):
        if (
            isinstance(item.data, MessageData)
            and item.data.role == "user"
            and not item.data.is_meta
        ):
            human_messages += 1
            # Never transfer a subsequent prompt/history to a prompt-only Jev comparison.
            if human_messages > 1:
                return None
            if (
                any(block.get("type") != "input_text" for block in item.data.content)
                or "\n".join(block.get("text", "") for block in item.data.content).strip()
                != frozen.task.strip()
            ):
                return None
        if item.response_id == response_id:
            if item.type in {"function_call", "function_call_output", "compaction", "error"}:
                return None
            if isinstance(item.data, MessageData) and item.data.role == "assistant":
                if item.status != "completed" or item.data.interrupted:
                    return None
                if item.data.is_meta:
                    continue
                for block in item.data.content:
                    if block.get("type") != "output_text":
                        return None
                    answer_parts.append(block.get("text", ""))
                seen_answer = True
    answer = "\n\n".join(answer_parts).strip()
    if human_messages != 1 or not seen_answer or not answer or len(answer) > MAX_ANSWER_CHARS:
        return None
    other_id = (
        review.advisor_choice_id
        if source_choice.choice_id == review.human_choice_id
        else review.human_choice_id
    )
    other = next(c for c in frozen.pool if c.choice_id == other_id)
    return {
        "host_id": conv.host_id,
        "round_id": round_id,
        "frozen": frozen,
        "other": other,
        "source": {
            "arm": "human" if source_choice.choice_id == review.human_choice_id else "jev",
            "choice_id": source_choice.choice_id,
            "model": source_choice.model_id,
            "reasoning_effort": source_choice.reasoning_effort,
            "answer": answer,
            "attribution": attribution,
        },
    }


def _choice_id(payload: dict[str, Any]) -> str | None:
    from omnigent.model_advisor_provider_policy import LogicalChoice

    return LogicalChoice(**payload).choice_id if payload else None


def result_payload(source: dict[str, Any], route: Any, answer: str, overhead: dict) -> dict:
    other = source["other"]
    alternate = {
        "arm": "jev" if source["source"]["arm"] == "human" else "human",
        "choice_id": other.choice_id,
        "model": other.model_id,
        "reasoning_effort": other.reasoning_effort,
        "answer": answer,
        "requested_route": route.to_payload(),
        # The transport provides usage, not verified upstream model identity.
        "actual_model": None,
        "overhead": overhead,
    }
    return {
        "state": "ready",
        "round_id": source["round_id"],
        "task_digest": hashlib.sha256(source["frozen"].task.encode()).hexdigest(),
        "context_scope": "initial_prompt_only",
        "presentation": "original_answer_then_pair",
        "candidates": [source["source"], alternate],
    }


def alternative_plan(source: dict[str, Any], catalog: Any):
    frozen, choice = source["frozen"], source["other"]
    plan = plan_transport(
        choice,
        frozen.preference_for(choice.provider),
        catalog.routes_by_choice.get(choice.choice_id, ()),
    )
    if plan.primary.choice.harness != "codex-native":
        raise ValueError("The other choice needs a tool-free transport that is not yet supported")
    supported = {
        ("openai", "codex-direct"): {"codex-login"},
        ("openai", "omniroute"): {"omniroute-codex-oauth"},
        ("glm", "glm-direct"): {"zai-direct-coding-plan", "zai-direct"},
        ("glm", "omniroute"): {"omniroute-glm-coding-plan"},
    }
    if plan.primary.connection_id not in supported.get(
        (choice.provider, plan.primary.route_id), set()
    ):
        raise ValueError("The comparison transport cannot attest the frozen account binding")
    return plan


def public_comparison(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    candidates = list(record.get("candidates", []))
    if record.get("reverse"):
        candidates.reverse()
    voted = record.get("vote") is not None
    return {
        "state": record["state"],
        "revision": record["revision"],
        "error": record.get("error"),
        "reason": record.get("reason"),
        "vote": record.get("vote"),
        "candidates": [
            {
                "slot": slot,
                "answer": candidate["answer"],
                **(
                    {
                        key: candidate.get(key)
                        for key in ("arm", "model", "reasoning_effort", "attribution")
                    }
                    if voted
                    else {}
                ),
            }
            for slot, candidate in zip(("a", "b"), candidates, strict=False)
        ],
    }


def save_vote(
    store: ConversationStore,
    session_id: str,
    response_id: str,
    actor: str,
    choice: Vote,
    comment: str,
) -> dict[str, Any]:
    record = read_comparison(store, session_id, response_id, actor)
    if record is None or record.get("state") != "ready":
        raise ValueError("Wait for both answers before choosing")
    candidates = list(record["candidates"])
    if record.get("reverse"):
        candidates.reverse()
    arm = candidates[0 if choice == "a" else 1]["arm"] if choice in {"a", "b"} else None
    append_event(
        store,
        session_id,
        response_id,
        actor,
        {"vote": {"choice": choice, "preferred_arm": arm, "comment": comment}},
    )
    return read_comparison(store, session_id, response_id, actor) or {}
