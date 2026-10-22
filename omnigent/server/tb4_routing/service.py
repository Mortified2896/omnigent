"""Select concrete routes from the live catalogue with fail-closed cost evidence."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError


class TB4Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    minimum_score: float = Field(default=25, ge=0, le=100)
    cost_mode: Literal["free", "included", "metered"] = "included"
    max_input_usd_per_million: float = Field(default=1, ge=0, le=1000)
    max_output_usd_per_million: float = Field(default=5, ge=0, le=10000)
    minimum_context_tokens: int = Field(default=64000, ge=16000, le=2000000)
    image_input: bool = False


def enabled() -> bool:
    return os.environ.get("OMNIGENT_TB4_ROUTING_ENABLED") == "1"


def benchmarks() -> dict:
    return json.loads(Path(__file__).with_name("benchmarks.json").read_text())


def readiness() -> dict:
    path = os.environ.get("OMNIGENT_TB4_ROUTE_EVIDENCE")
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if not 0 <= time.time() - data.get("observed_at", 0) <= 1200:
        return {}  # Stale account/entitlement evidence must not authorize a new route.
    return data


def rate(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def merge_responses_health(evidence: dict, rows: list[dict]) -> dict:
    """Overlay fresh, exact request outcomes without counting active calls or aliases."""
    routes = dict(evidence.get("routes", {}))
    for row in rows:
        route = row.get("requestedModel")
        status = rate(row.get("status"))
        if not route or row.get("path") != "/v1/responses" or status is None or status < 200:
            continue
        try:
            observed = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
        if not 0 <= time.time() - observed <= 86400:
            continue
        if observed >= (rate(routes.get(route, {}).get("observed_at")) or 0):
            routes[route] = {
                "status": status,
                "observed_at": observed,
                "endpoint": "/v1/responses",
            }
    return {**evidence, "routes": routes}


def evaluate(policy: TB4Policy, catalogue: list[dict], evidence: dict, registry: dict) -> dict:
    by_model: dict[str, list[dict]] = {}
    for row in registry["models"]:
        by_model.setdefault(row["model"].casefold(), []).append(row)
    excluded: Counter = Counter()
    eligible = []
    providers = evidence.get("providers", {})
    routes = evidence.get("routes", {})
    for row in catalogue:
        route = row.get("id", "")
        provider, separator, model = route.partition("/")
        # Canonical provider IDs only: aliases and custom combos can hide fallbacks.
        if not separator or provider != row.get("owned_by") or provider == "custom":
            excluded["Alias or virtual route"] += 1
            continue
        account = providers.get(provider, {})
        if not account.get("active"):
            excluded["No fresh active account evidence"] += 1
            continue
        identity = model.rsplit("/", 1)[-1].casefold().removesuffix(":free")
        records = by_model.get(identity, [])
        if not records:
            excluded["No exact published TB4 model identity"] += 1
            continue
        capabilities = row.get("capabilities") or {}
        if capabilities.get("tool_calling") is not True:
            excluded["Tool calling unavailable"] += 1
            continue
        if (rate(row.get("context_length")) or 0) < policy.minimum_context_tokens:
            excluded["Context window too small or unknown"] += 1
            continue
        if policy.image_input and capabilities.get("vision") is not True:
            excluded["Image input unavailable"] += 1
            continue
        recent = routes.get(route, {})
        if (rate(recent.get("status")) or 0) >= 400:
            excluded["Latest observed request failed"] += 1
            continue
        if (
            rate(recent.get("status")) != 200
            or recent.get("endpoint") != "/v1/responses"
            or not 0 <= time.time() - (rate(recent.get("observed_at")) or 0) <= 86400
        ):
            excluded["No verified recent Responses success"] += 1
            continue
        # An explicit :free route is a public zero-price offering. Account quotas
        # remain limits, not permission to spill to a paid model.
        cost_class = "free" if model.endswith(":free") else account.get("cost_class", "unknown")
        pricing = row.get("pricing") or {}
        input_rate, output_rate = rate(pricing.get("input")), rate(pricing.get("output"))
        if cost_class not in ("free", "subscription"):
            cost_class = (
                "metered" if input_rate is not None and output_rate is not None else "unknown"
            )
        allowed_cost = cost_class == "free" or (
            policy.cost_mode != "free" and cost_class == "subscription"
        )
        if policy.cost_mode == "metered" and cost_class == "metered":
            allowed_cost = (
                input_rate is not None
                and output_rate is not None
                and input_rate <= policy.max_input_usd_per_million
                and output_rate <= policy.max_output_usd_per_million
            )
        if not allowed_cost:
            excluded["Cost outside policy or unknown"] += 1
            continue
        tiers = capabilities.get("effort_tiers") or row.get("effort_tiers") or []
        matching = [
            r
            for r in records
            if r["score"] >= policy.minimum_score
            and "fallback" not in r["configuration"].casefold()
            and (not r.get("requires_reasoning") or capabilities.get("reasoning") is True)
            and (r["effort"] == "default" or r["effort"] in tiers)
        ]
        if not matching:
            excluded["Below TB4 floor or effort unavailable"] += 1
            continue
        best = max(matching, key=lambda r: r["score"])
        eligible.append(
            {
                "route": route,
                "model": best["model"],
                "name": best["name"],
                "score": best["score"],
                "effort": best["effort"],
                "published_configuration": best["configuration"],
                "cost_class": cost_class,
                "input_usd_per_million": input_rate,
                "output_usd_per_million": output_rate,
                "health": "recent Responses success",
                "source_url": best["source_url"],
                "context_tokens": row.get("context_length"),
            }
        )
    eligible.sort(
        key=lambda r: (
            {"free": 0, "subscription": 1, "metered": 2}[r["cost_class"]],
            r["health"] != "recent Responses success",
            r["input_usd_per_million"] or 0 if r["cost_class"] == "metered" else 0,
            -r["score"],
            r["route"],
        )
    )
    return {
        "enabled": True,
        "policy": policy.model_dump(),
        "eligible": eligible,
        "eligible_models": len({r["model"] for r in eligible}),
        "eligible_routes": len(eligible),
        "catalogue_routes": len(catalogue),
        "exclusions": dict(excluded),
        "benchmark": {k: v for k, v in registry.items() if k != "models"},
        "account_evidence_at": evidence.get("observed_at"),
        "selection_rule": (
            "Free quota first, then included subscription, then eligible token rates; "
            "recent health and TB4 break ties."
        ),
    }


async def catalogue(policy: TB4Policy) -> dict:
    if not enabled():
        raise OmnigentError("TB4 routing is not enabled on this server", code=ErrorCode.CONFLICT)
    try:
        client = OmniRouteClient.from_env()
        # Read-only checks at preview and both launch boundaries close the gap
        # between scheduled refreshes. A listing/healthy account alone is not
        # evidence that this particular model works through Codex's endpoint.
        async with asyncio.timeout(15):
            response, recent = await asyncio.gather(
                client._request("GET", "/v1/models", timeout=15),
                client.list_call_logs(limit=100, offset=0),
            )
        rows = response.body.get("data")
        if not isinstance(rows, list):
            raise ValueError("missing catalogue")
        return evaluate(policy, rows, merge_responses_health(readiness(), recent), benchmarks())
    except (OmniRouteError, OSError, ValueError, TimeoutError) as exc:
        raise OmnigentError(
            "The live route catalogue or account evidence is unavailable. "
            "Try again after routing health refresh succeeds.",
            code=ErrorCode.CONFLICT,
        ) from exc


async def bind_policy(body, agent_name: str):
    """Validate on the server immediately before launch; clients cannot choose a bypass."""
    if any(k.startswith("omnigent.tb4.") for k in body.labels):
        raise OmnigentError("TB4 receipt labels are server-owned", code=ErrorCode.INVALID_INPUT)
    if body.tb4_policy is None:
        if body.labels.get("omnigent.routing_policy") == "tb4":
            raise OmnigentError("TB4 policy is required", code=ErrorCode.INVALID_INPUT)
        return body
    if (
        agent_name != "codex-native-ui"
        or body.host_type != "external"
        or body.parent_session_id
        or body.harness_override not in (None, "codex-native")
    ):
        raise OmnigentError(
            "TB4 routing currently requires a new Codex chat on an external host",
            code=ErrorCode.INVALID_INPUT,
        )
    if body.model_override or body.reasoning_effort or body.cost_control_mode_override == "on":
        raise OmnigentError(
            "TB4 routing owns the model and effort; clear other routing choices",
            code=ErrorCode.INVALID_INPUT,
        )
    if any(item.model_override is not None for item in body.initial_items):
        raise OmnigentError(
            "TB4 routing owns initial-turn model choices", code=ErrorCode.INVALID_INPUT
        )
    result = await catalogue(body.tb4_policy)
    candidates = result["eligible"]
    if not candidates:
        raise OmnigentError(
            "No verified OmniRoute route meets this TB4 floor and cost policy. "
            "Unavailable or unverified routes cannot be enabled by lowering the floor. "
            "Try again later or explicitly change the cost policy; no fallback was started.",
            code=ErrorCode.CONFLICT,
        )
    selected = candidates[0]
    receipt = uuid.uuid4().hex
    # Per-selection immutable audit file, without prompt or credentials.
    root = Path(
        os.environ.get("OMNIGENT_TB4_AUDIT_DIR", str(Path.home() / ".omnigent" / "tb4-routing"))
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / (receipt + ".json")).open("x") as handle:
        os.chmod(handle.name, 0o600)
        json.dump(
            {"id": receipt, "created_at": time.time(), "selected": selected, **result}, handle
        )
    return body.model_copy(
        update={
            "model_override": selected["route"],
            "reasoning_effort": None if selected["effort"] == "default" else selected["effort"],
            "cost_control_mode_override": "off",
            "subagent_routing_override": "off",
            "labels": {
                **body.labels,
                "omnigent.routing_policy": "tb4",
                "omnigent.access_lane": "omniroute",
                "omnigent.tb4.receipt": receipt,
                "omnigent.tb4.score": str(selected["score"]),
                "omnigent.tb4.policy": body.tb4_policy.model_dump_json(),
                "omnigent.tb4.route": selected["route"],
            },
        }
    )


async def validate_pinned_launch(
    policy: dict, route: str | None, effort: str | None, endpoint: str | None
) -> dict:
    """The broad gateway catalogue, not Codex's vendor list, owns TB4 routes."""
    client = OmniRouteClient.from_env()
    expected = client.base_url.rstrip("/").removesuffix("/v1") + "/v1"
    if not endpoint or endpoint.rstrip("/") != expected:
        raise RuntimeError("TB4 launch does not use the verified OmniRoute endpoint")
    result = await catalogue(TB4Policy.model_validate(policy))
    selected = next(
        (
            row
            for row in result["eligible"]
            if row["route"] == route and row["effort"] == (effort or "default")
        ),
        None,
    )
    if selected is None:
        raise RuntimeError("The pinned TB4 route is no longer eligible; no fallback was started")
    return selected
