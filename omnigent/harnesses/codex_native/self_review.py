"""Ephemeral, read-only self-review for completed native Codex turns.

The reviewer forks the exact completed turn instead of steering the user's
thread.  That preserves the parent transcript and gives Codex/provider caches
the largest possible identical prefix.  Cache reuse is observed from Codex's
usage notification; missing counters remain ``None`` rather than being inferred.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.entities.conversation import ResourceEventData
from omnigent.harnesses.codex_native.app_server import CodexAppServerClient, client_for_transport
from omnigent.harnesses.codex_native.bridge import read_bridge_state, read_policy_hook_config
from omnigent.server.task_experiment import RESOURCE_TYPE, normalize_tags

_logger = logging.getLogger(__name__)

SELF_REVIEW_ENV = "OMNIGENT_TASK_SELF_REVIEW"
SELF_REVIEW_PROMPT_VERSION = "codex-ephemeral-self-review-v1"
SELF_REVIEW_TIMEOUT_SECONDS = 120.0
PRIMARY_TERMINAL_TIMEOUT_SECONDS = 60 * 60.0

_REVIEW_TAGS = (
    "AGENTS instructions",
    "Documentation",
    "Task specification",
    "Routing/floor",
    "Model capability",
    "Tool/harness",
    "Environment/dependency",
    "Tests/verification",
)
_TOOL_ITEM_TYPES = frozenset(
    {
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "subAgentActivity",
        "webSearch",
        "imageView",
        "imageGeneration",
    }
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodexSelfReview(_StrictModel):
    """Strict output contract shared with the UI/storage experiment."""

    outcome: Literal["success", "partial", "failed", "not_sure"]
    confidence: float = Field(ge=0, le=1)
    comment: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=3)
    evidence: list[str] = Field(default_factory=list, max_length=3)


class ReviewUsage(_StrictModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


class CompletedSelfReview(_StrictModel):
    review: CodexSelfReview
    fork_thread_id: str
    review_turn_id: str
    model: str | None = None
    model_provider: str | None = None
    reasoning_effort: str | None = None
    usage: ReviewUsage = Field(default_factory=ReviewUsage)


def self_review_enabled() -> bool:
    value = os.environ.get(SELF_REVIEW_ENV, "").strip().casefold()
    return value in {"1", "true", "yes", "on"}


def _object(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _turn_id(params: dict[str, object]) -> str | None:
    direct = _string(params.get("turnId"))
    if direct:
        return direct
    turn = _object(params.get("turn"))
    return _string(turn.get("id")) if turn else None


def _thread_id(params: dict[str, object]) -> str | None:
    return _string(params.get("threadId"))


def _usage_from_params(params: dict[str, object]) -> ReviewUsage | None:
    token_usage = _object(params.get("tokenUsage"))
    last = _object(token_usage.get("last")) if token_usage else None
    if last is None:
        return None

    def integer(name: str) -> int | None:
        value = last.get(name)
        return value if isinstance(value, int) and value >= 0 else None

    return ReviewUsage(
        input_tokens=integer("inputTokens"),
        output_tokens=integer("outputTokens"),
        reasoning_tokens=integer("reasoningOutputTokens"),
        cache_read_tokens=integer("cachedInputTokens"),
        cache_write_tokens=integer("cacheWriteInputTokens"),
    )


def _review_prompt() -> str:
    tags = ", ".join(_REVIEW_TAGS)
    return (
        "Evaluate ONLY the user task and the work already completed in this forked history. "
        "Do not fix, retry, edit, browse, run commands, invoke tools, or ask follow-up questions. "
        "Return whether the ORIGINAL attempt was success, partial, failed, or not_sure. "
        "Success means it accomplished the requested task without a material correction or retry; "
        "partial means meaningful correct progress but a material follow-up/correction remains; "
        "failed means it did not accomplish the task or sufficient correct progress; not_sure means "
        "the existing evidence cannot reliably establish the outcome. Give one concise comment, up "
        "to three short evidence statements, and up to three relevant tags. Prefer these tags when "
        f"applicable: {tags}. Judge the completed attempt, not this review instruction."
    )


async def wait_for_primary_terminal(
    bridge_dir: Path,
    *,
    session_id: str,
    primary_turn_id: str,
    timeout_seconds: float = PRIMARY_TERMINAL_TIMEOUT_SECONDS,
) -> bool:
    """Wait until the forwarder no longer records *primary_turn_id* as active."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        state = read_bridge_state(bridge_dir)
        if state is None or state.session_id != session_id:
            return False
        if state.active_turn_id != primary_turn_id:
            return True
        await asyncio.sleep(0.2)
    return False


async def run_self_review(
    *,
    socket_path: str,
    parent_thread_id: str,
    primary_turn_id: str,
    timeout_seconds: float = SELF_REVIEW_TIMEOUT_SECONDS,
) -> CompletedSelfReview | None:
    """Fork one completed turn and return its tool-free structured review."""
    client: CodexAppServerClient = client_for_transport(
        socket_path,
        client_name="omnigent-codex-self-review",
    )
    await client.connect()
    try:
        fork_response = await client.request(
            "thread/fork",
            {
                "threadId": parent_thread_id,
                "lastTurnId": primary_turn_id,
                "ephemeral": True,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "excludeTurns": True,
            },
        )
        fork_result = _object(fork_response.get("result"))
        thread = _object(fork_result.get("thread")) if fork_result else None
        fork_thread_id = _string(thread.get("id")) if thread else None
        if fork_thread_id is None:
            raise ValueError("Codex thread/fork returned no thread id")

        start_response = await client.request(
            "turn/start",
            {
                "threadId": fork_thread_id,
                "input": [{"type": "text", "text": _review_prompt()}],
                "approvalPolicy": "never",
                "outputSchema": CodexSelfReview.model_json_schema(),
            },
        )
        start_result = _object(start_response.get("result"))
        turn = _object(start_result.get("turn")) if start_result else None
        review_turn_id = _string(turn.get("id")) if turn else None
        if review_turn_id is None:
            raise ValueError("Codex turn/start returned no review turn id")

        final_text: str | None = None
        usage = ReviewUsage()
        tool_activity = False
        completed = False
        async with asyncio.timeout(timeout_seconds):
            async for event in client.iter_events():
                method = event.get("method")
                params = _object(event.get("params"))
                if not isinstance(method, str) or params is None:
                    continue
                if _thread_id(params) not in {None, fork_thread_id}:
                    continue
                event_turn_id = _turn_id(params)
                if event_turn_id not in {None, review_turn_id}:
                    continue
                if method == "thread/tokenUsage/updated":
                    observed = _usage_from_params(params)
                    if observed is not None:
                        usage = observed
                    continue
                if method == "item/completed":
                    item = _object(params.get("item"))
                    if item is None:
                        continue
                    item_type = item.get("type")
                    if isinstance(item_type, str) and item_type in _TOOL_ITEM_TYPES:
                        tool_activity = True
                    if item_type == "agentMessage":
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            final_text = text
                    continue
                if method == "turn/completed" and event_turn_id == review_turn_id:
                    completed = True
                    break
                if method in {"turn/failed", "error"} and event_turn_id in {
                    None,
                    review_turn_id,
                }:
                    return None

        if not completed or tool_activity or final_text is None:
            return None
        try:
            review = CodexSelfReview.model_validate(json.loads(final_text))
        except (json.JSONDecodeError, ValidationError, TypeError):
            return None
        review = review.model_copy(
            update={
                "tags": normalize_tags(review.tags[:3]),
                "evidence": [
                    " ".join(value.split())[:500]
                    for value in review.evidence[:3]
                    if value.strip()
                ],
                "comment": review.comment.strip(),
            }
        )
        return CompletedSelfReview(
            review=review,
            fork_thread_id=fork_thread_id,
            review_turn_id=review_turn_id,
            model=_string(fork_result.get("model")) if fork_result else None,
            model_provider=_string(fork_result.get("modelProvider")) if fork_result else None,
            reasoning_effort=(
                _string(fork_result.get("reasoningEffort")) if fork_result else None
            ),
            usage=usage,
        )
    finally:
        await client.close()


async def persist_self_review(
    bridge_dir: Path,
    *,
    session_id: str,
    primary_thread_id: str,
    primary_turn_id: str,
    completed: CompletedSelfReview,
) -> bool:
    """Persist a system-owned model-review resource event through runner auth."""
    config = read_policy_hook_config(bridge_dir)
    if config is None:
        return False
    base_url = config.get("ap_server_url")
    headers = config.get("ap_auth_headers")
    if not isinstance(base_url, str) or not base_url:
        return False
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        return False

    response_id = f"codex_{primary_turn_id}"
    review = completed.review
    resource = {
        "schema_version": 1,
        "conversation_id": session_id,
        "attempt_id": response_id,
        "kind": "model_review",
        "outcome": review.outcome,
        "confidence": review.confidence,
        "comment": review.comment,
        "tags": review.tags,
        "evidence": review.evidence,
        "review_source": "model",
        "provenance": {
            "review_strategy": "ephemeral_thread_fork",
            "review_prompt_version": SELF_REVIEW_PROMPT_VERSION,
            "primary_thread_id": primary_thread_id,
            "primary_turn_id": primary_turn_id,
            "fork_thread_id": completed.fork_thread_id,
            "review_turn_id": completed.review_turn_id,
            "ephemeral": True,
            "sandbox": "read-only",
            "model": completed.model,
            "model_provider": completed.model_provider,
            "reasoning_effort": completed.reasoning_effort,
            "token_usage": completed.usage.model_dump(mode="json"),
        },
    }
    item_data = ResourceEventData(
        event_type="task.experiment.model_review",
        resource_id=response_id,
        resource_type=RESOURCE_TYPE,
        resource=resource,
    ).model_dump(mode="json")
    payload = {
        "type": "external_conversation_item",
        "data": {
            "item_type": "resource_event",
            "item_data": item_data,
            "response_id": response_id,
            "source_id": f"task-model-review:{response_id}:{SELF_REVIEW_PROMPT_VERSION}",
        },
    }
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={str(key): str(value) for key, value in headers.items()},
        timeout=30.0,
    ) as ap_client:
        response = await ap_client.post(
            f"/v1/sessions/{session_id}/events",
            json=payload,
        )
    if response.status_code >= 400:
        _logger.warning(
            "Codex self-review persistence failed: session=%s status=%s",
            session_id,
            response.status_code,
        )
        return False
    return True


async def review_completed_turn(
    bridge_dir: Path,
    *,
    session_id: str,
    parent_thread_id: str,
    primary_turn_id: str,
) -> bool:
    """Non-blocking task body used by the native executor after turn acceptance."""
    try:
        if not await wait_for_primary_terminal(
            bridge_dir,
            session_id=session_id,
            primary_turn_id=primary_turn_id,
        ):
            return False
        state = read_bridge_state(bridge_dir)
        if state is None or state.session_id != session_id:
            return False
        completed = await run_self_review(
            socket_path=state.socket_path,
            parent_thread_id=parent_thread_id,
            primary_turn_id=primary_turn_id,
        )
        if completed is None:
            return False
        return await persist_self_review(
            bridge_dir,
            session_id=session_id,
            primary_thread_id=parent_thread_id,
            primary_turn_id=primary_turn_id,
            completed=completed,
        )
    except Exception:  # noqa: BLE001 - evaluation must never fail the user's task.
        _logger.warning(
            "Codex self-review failed non-fatally: session=%s turn=%s",
            session_id,
            primary_turn_id,
            exc_info=True,
        )
        return False
