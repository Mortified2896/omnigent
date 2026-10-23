"""Omnigent owns task constraints and route pinning; OmniRoute owns eligibility."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient

from .availability import gateway_request


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


async def catalogue(policy: TB4Policy) -> dict:
    if not enabled():
        raise OmnigentError("TB4 routing is not enabled on this server", code=ErrorCode.CONFLICT)
    result = await gateway_request("POST", "/selection", policy.model_dump())
    if not isinstance(result.get("eligible"), list) or result.get("policy") != policy.model_dump():
        raise OmnigentError("Invalid OmniRoute selection response", code=ErrorCode.CONFLICT)
    return result


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
