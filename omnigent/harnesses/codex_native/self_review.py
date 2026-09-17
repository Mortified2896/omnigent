"""Ephemeral, read-only self-review for completed native Codex turns.

The reviewer forks the exact completed turn instead of steering the user's
thread. That preserves the parent transcript and gives Codex/provider caches the
largest possible identical prefix. When the primary task ran through an owned
OmniRoute O3 Combo, the reviewer best-effort pins the fork to the direct route
that actually served the primary call and then verifies the observed review
backend from fresh call logs. Cache reuse is observed only from Codex's usage
notification; missing counters remain ``None`` rather than being inferred.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.entities.conversation import ResourceEventData
from omnigent.harnesses.codex_native.app_server import CodexAppServerClient, client_for_transport
from omnigent.harnesses.codex_native.bridge import read_bridge_state, read_policy_hook_config
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError
from omnigent.server.task_experiment import RESOURCE_TYPE, normalize_tags

_logger = logging.getLogger(__name__)

SELF_REVIEW_ENV = "OMNIGENT_TASK_SELF_REVIEW"
SELF_REVIEW_PROMPT_VERSION = "codex-ephemeral-self-review-v1"
SELF_REVIEW_TIMEOUT_SECONDS = 120.0
PRIMARY_TERMINAL_TIMEOUT_SECONDS = 60 * 60.0
_OMNIROUTE_LOOKBACK = timedelta(hours=2)
_OWNED_ROUTE_PREFIX = "custom/o3-route-"

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


class ReviewBackendAffinity(_StrictModel):
    """Measured primary/review backend identity for cache-affinity analysis."""

    attempted: bool = False
    parent_route: str | None = None
    primary_call_log_id: str | None = None
    primary_provider: str | None = None
    primary_model: str | None = None
    primary_connection_id: str | None = None
    pinned_route: str | None = None
    review_call_log_id: str | None = None
    review_provider: str | None = None
    review_model: str | None = None
    review_connection_id: str | None = None
    model_provider_match: bool | None = None
    connection_match: bool | None = None
    note: str | None = None


class CompletedSelfReview(_StrictModel):
    review: CodexSelfReview
    fork_thread_id: str
    review_turn_id: str
    model: str | None = None
    model_provider: str | None = None
    reasoning_effort: str | None = None
    usage: ReviewUsage = Field(default_factory=ReviewUsage)
    backend_affinity: ReviewBackendAffinity = Field(default_factory=ReviewBackendAffinity)


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


def _cache_provenance(usage: ReviewUsage) -> dict[str, object | None]:
    ratio: float | None = None
    if (
        usage.input_tokens is not None
        and usage.input_tokens > 0
        and usage.cache_read_tokens is not None
    ):
        ratio = usage.cache_read_tokens / usage.input_tokens
    return {
        "reuse_observed": (
            None if usage.cache_read_tokens is None else usage.cache_read_tokens > 0
        ),
        "cache_read_ratio": ratio,
        "measurement_source": "codex thread/tokenUsage/updated",
        # A fork gives the backend an identical-prefix opportunity, but the
        # installed Codex/app-server version must be acceptance-tested before
        # claiming that it preserves the parent's prompt_cache_key lineage.
        "prompt_cache_lineage_verified": None,
    }


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


def _claim_self_review_once(bridge_dir: Path, *, session_id: str, primary_turn_id: str) -> bool:
    """Atomically claim one evaluator call for a primary turn across both scheduling paths."""
    digest = hashlib.sha256(f"{session_id}:{primary_turn_id}".encode()).hexdigest()[:24]
    path = bridge_dir / f"self-review-{digest}.claim"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{session_id}\n{primary_turn_id}\n")
    return True


def _bare_model(provider: str, model: str) -> str:
    prefix = f"{provider}/"
    if model.startswith(prefix):
        return model[len(prefix) :]
    return model.rsplit("/", 1)[-1]


def _select_affinity_target(
    combo: dict[str, object],
    *,
    provider: str,
    model: str,
    connection_id: str | None,
) -> str | None:
    """Return the unique direct model route matching an observed Combo target."""
    raw_models = combo.get("models")
    if not isinstance(raw_models, list):
        return None
    matches: list[dict[str, object]] = []
    for item in raw_models:
        if not isinstance(item, dict) or item.get("providerId") != provider:
            continue
        candidate = item.get("model")
        if not isinstance(candidate, str) or not candidate:
            continue
        if _bare_model(provider, candidate) != _bare_model(provider, model):
            continue
        matches.append(item)
    if connection_id:
        exact = [item for item in matches if item.get("connectionId") == connection_id]
        if len(exact) == 1:
            candidate = exact[0].get("model")
            return candidate if isinstance(candidate, str) else None
    if len(matches) != 1:
        return None
    candidate = matches[0].get("model")
    return candidate if isinstance(candidate, str) else None


async def _latest_route_call(
    client: OmniRouteClient,
    route: str,
    *,
    not_before: datetime,
    not_after: datetime,
) -> dict[str, object] | None:
    """Return the latest successful Responses call for one route in a bounded window."""
    rows = await client.list_call_logs(limit=100, offset=0)
    matches: list[tuple[datetime, dict[str, object]]] = []
    for row in rows:
        if row.get("path") != "/v1/responses" or row.get("method") != "POST":
            continue
        if row.get("status") != 200:
            continue
        if route not in {row.get("comboName"), row.get("requestedModel")}:
            continue
        timestamp = client._parse_timestamp(row.get("timestamp"))  # noqa: SLF001
        if timestamp is None or timestamp < not_before or timestamp > not_after:
            continue
        provider = _string(row.get("provider"))
        model = _string(row.get("model"))
        call_log_id = _string(row.get("id"))
        if provider is None or model is None or call_log_id is None:
            continue
        matches.append((timestamp, row))
    if not matches:
        return None
    timestamp, row = max(matches, key=lambda item: (item[0], str(item[1].get("id", ""))))
    return {
        "call_log_id": row["id"],
        "provider": row["provider"],
        "model": row["model"],
        "connection_id": _string(row.get("connectionId")),
        "timestamp": timestamp,
    }


async def _prepare_backend_affinity(
    *,
    parent_route: str | None,
    now: datetime,
) -> tuple[OmniRouteClient | None, ReviewBackendAffinity]:
    affinity = ReviewBackendAffinity(parent_route=parent_route)
    if parent_route is None or not parent_route.startswith(_OWNED_ROUTE_PREFIX):
        return None, affinity.model_copy(
            update={"note": "primary thread is not using an owned O3 OmniRoute Combo"}
        )
    try:
        omniroute = OmniRouteClient.from_env()
        primary = await _latest_route_call(
            omniroute,
            parent_route,
            not_before=now - _OMNIROUTE_LOOKBACK,
            not_after=now,
        )
        if primary is None:
            return omniroute, affinity.model_copy(
                update={
                    "attempted": True,
                    "note": "no recent successful primary OmniRoute call could be correlated",
                }
            )
        combo = await omniroute.get_combo(parent_route)
        if combo is None:
            return omniroute, affinity.model_copy(
                update={
                    "attempted": True,
                    "primary_call_log_id": primary["call_log_id"],
                    "primary_provider": primary["provider"],
                    "primary_model": primary["model"],
                    "primary_connection_id": primary["connection_id"],
                    "note": "owned primary Combo disappeared before review",
                }
            )
        pinned_route = _select_affinity_target(
            combo,
            provider=str(primary["provider"]),
            model=str(primary["model"]),
            connection_id=(
                str(primary["connection_id"]) if primary["connection_id"] is not None else None
            ),
        )
        note = (
            "fork will request the direct route that served the primary call; "
            "connection equality is verified after review"
            if pinned_route
            else "primary backend could not be mapped unambiguously to one direct Combo target"
        )
        return omniroute, ReviewBackendAffinity(
            attempted=True,
            parent_route=parent_route,
            primary_call_log_id=str(primary["call_log_id"]),
            primary_provider=str(primary["provider"]),
            primary_model=str(primary["model"]),
            primary_connection_id=(
                str(primary["connection_id"]) if primary["connection_id"] is not None else None
            ),
            pinned_route=pinned_route,
            note=note,
        )
    except (OmniRouteError, OSError, ValueError, TypeError) as exc:
        return None, affinity.model_copy(
            update={"attempted": True, "note": f"backend affinity unavailable: {exc}"}
        )


async def _verify_backend_affinity(
    client: OmniRouteClient | None,
    affinity: ReviewBackendAffinity,
    *,
    route_used: str | None,
    not_before: datetime,
    not_after: datetime,
) -> ReviewBackendAffinity:
    if client is None or route_used is None:
        return affinity
    try:
        observed = await _latest_route_call(
            client,
            route_used,
            not_before=not_before,
            not_after=not_after,
        )
    except (OmniRouteError, OSError, ValueError, TypeError):
        return affinity
    if observed is None:
        return affinity.model_copy(
            update={"note": f"{affinity.note or ''}; review backend call was not observable".strip("; ")}
        )
    provider_match = (
        affinity.primary_provider == observed["provider"]
        and affinity.primary_model is not None
        and _bare_model(str(observed["provider"]), affinity.primary_model)
        == _bare_model(str(observed["provider"]), str(observed["model"]))
    )
    connection_match: bool | None = None
    if affinity.primary_connection_id is not None and observed["connection_id"] is not None:
        connection_match = affinity.primary_connection_id == observed["connection_id"]
    return affinity.model_copy(
        update={
            "review_call_log_id": str(observed["call_log_id"]),
            "review_provider": str(observed["provider"]),
            "review_model": str(observed["model"]),
            "review_connection_id": (
                str(observed["connection_id"]) if observed["connection_id"] is not None else None
            ),
            "model_provider_match": provider_match,
            "connection_match": connection_match,
        }
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

        parent_route = _string(fork_result.get("model")) if fork_result else None
        omniroute, affinity = await _prepare_backend_affinity(
            parent_route=parent_route,
            now=datetime.now(timezone.utc),
        )
        route_used = parent_route
        if affinity.pinned_route is not None:
            try:
                await client.request(
                    "thread/settings/update",
                    {"threadId": fork_thread_id, "model": affinity.pinned_route},
                )
                route_used = affinity.pinned_route
            except Exception as exc:  # noqa: BLE001 - affinity is best-effort, review is optional.
                affinity = affinity.model_copy(
                    update={
                        "pinned_route": None,
                        "note": f"direct-route pin failed; review used inherited route: {exc}",
                    }
                )

        review_started_at = datetime.now(timezone.utc)
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
                    turn_data = _object(params.get("turn"))
                    status = _string(turn_data.get("status")) if turn_data else None
                    if status not in {None, "completed"}:
                        return None
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
        affinity = await _verify_backend_affinity(
            omniroute,
            affinity,
            route_used=route_used,
            not_before=review_started_at - timedelta(seconds=2),
            not_after=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
        return CompletedSelfReview(
            review=review,
            fork_thread_id=fork_thread_id,
            review_turn_id=review_turn_id,
            model=affinity.review_model
            or (_string(fork_result.get("model")) if fork_result else None),
            model_provider=affinity.review_provider
            or (_string(fork_result.get("modelProvider")) if fork_result else None),
            reasoning_effort=(
                _string(fork_result.get("reasoningEffort")) if fork_result else None
            ),
            usage=usage,
            backend_affinity=affinity,
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
            "cache": _cache_provenance(completed.usage),
            "backend_affinity": completed.backend_affinity.model_dump(mode="json"),
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
    """Non-blocking task body used by native terminal schedulers."""
    try:
        if not await wait_for_primary_terminal(
            bridge_dir,
            session_id=session_id,
            primary_turn_id=primary_turn_id,
        ):
            return False
        # Both the native executor and terminal hook can observe the same edge in
        # the current draft. Claim the evaluator durably before any model call so
        # retries or duplicate schedulers cannot charge/run it twice.
        if not _claim_self_review_once(
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
