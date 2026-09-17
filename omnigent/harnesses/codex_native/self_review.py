"""Fail-closed self-review and measurement contracts for native Codex turns.

Automated inference remains disabled until the installed protocol can prevent
all reviewer tools before side effects. A read-only sandbox and post-hoc tool
detection are insufficient. Missing cache/backend measurements remain unknown.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities.conversation import ResourceEventData
from omnigent.harnesses.codex_native.bridge import read_bridge_state, read_policy_hook_config
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError
from omnigent.server.task_experiment import RESOURCE_TYPE

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

    exact_backend_affinity_guaranteed: bool = False
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
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else None
        )

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


def _claim_self_review_once(bridge_dir: Path, *, session_id: str, primary_turn_id: str) -> bool:
    """Atomically claim one primary turn as a durable scheduling backstop."""
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


async def _correlated_route_call(
    client: OmniRouteClient,
    route: str,
    *,
    not_before: datetime,
    not_after: datetime,
    call_log_id: str | None,
) -> dict[str, object] | None:
    """Accept only a previously bound call id; time and route are extra checks."""
    if call_log_id is None:
        return None
    rows = await client.list_call_logs(limit=100, offset=0)
    matches: list[tuple[datetime, dict[str, object]]] = []
    for row in rows:
        if row.get("id") != call_log_id:
            continue
        if row.get("path") != "/v1/responses" or row.get("method") != "POST":
            continue
        if row.get("status") != 200:
            continue
        if route not in {row.get("comboName"), row.get("requestedModel")}:
            continue
        timestamp = client._parse_timestamp(row.get("timestamp"))
        if timestamp is None or timestamp < not_before or timestamp > not_after:
            continue
        provider = _string(row.get("provider"))
        model = _string(row.get("model"))
        call_log_id = _string(row.get("id"))
        if provider is None or model is None or call_log_id is None:
            continue
        matches.append((timestamp, row))
    if len(matches) != 1:
        return None
    timestamp, row = matches[0]
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
    primary_call_log_id: str | None = None,
) -> tuple[OmniRouteClient | None, ReviewBackendAffinity]:
    affinity = ReviewBackendAffinity(parent_route=parent_route)
    if parent_route is None or not parent_route.startswith(_OWNED_ROUTE_PREFIX):
        return None, affinity.model_copy(
            update={"note": "primary thread is not using an owned O3 OmniRoute Combo"}
        )
    try:
        omniroute = OmniRouteClient.from_env()
        primary = await _correlated_route_call(
            omniroute,
            parent_route,
            not_before=now - _OMNIROUTE_LOOKBACK,
            not_after=now,
            call_log_id=primary_call_log_id,
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
    review_call_log_id: str | None = None,
) -> ReviewBackendAffinity:
    if client is None or route_used is None:
        return affinity
    try:
        observed = await _correlated_route_call(
            client,
            route_used,
            not_before=not_before,
            not_after=not_after,
            call_log_id=review_call_log_id,
        )
    except (OmniRouteError, OSError, ValueError, TypeError):
        return affinity
    if observed is None:
        return affinity.model_copy(
            update={
                "note": f"{affinity.note or ''}; review backend call was not observable".strip(
                    "; "
                )
            }
        )
    provider_match = (
        None
        if affinity.primary_provider is None or affinity.primary_model is None
        else (
            affinity.primary_provider == observed["provider"]
            and affinity.primary_model is not None
            and _bare_model(str(observed["provider"]), affinity.primary_model)
            == _bare_model(str(observed["provider"]), str(observed["model"]))
        )
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
    socket_path: str,  # noqa: ARG001 - retained public call contract.
    parent_thread_id: str,
    primary_turn_id: str,
    timeout_seconds: float = SELF_REVIEW_TIMEOUT_SECONDS,  # noqa: ARG001
) -> CompletedSelfReview | None:
    """Fail closed until the fork protocol can prohibit every tool before execution."""
    _logger.warning(
        "Automated Codex review disabled: no verified tool-free fork contract; thread=%s turn=%s",
        parent_thread_id,
        primary_turn_id,
    )
    return None


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
        # Claim durably before any evaluator call so duplicate terminal edges
        # cannot execute a second review.
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
