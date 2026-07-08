"""Approval-gated execution route proposals for Omnigent sessions."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

ALLOWED_BILLING_CLASSES = ["subscription", "free", "local", "session", "no-cost"]
FORBIDDEN_BILLING_CLASSES = ["api_billed", "unknown"]
EXECUTION_FALLBACK_POLICY = "API-billed fallback forbidden; paid provider fallback forbidden; unknown billing blocked by default"
_ROUTER_CONFIG_ENV = "OMNIGENT_ROUTER_PROFILE_CONFIG"
_ROUTE_GATE_ENABLED_ENV = "OMNIGENT_ROUTE_APPROVAL_GATE"


class RouterProfile(BaseModel):
    """Structured routing recommender profile.

    ``model_id`` and ``provider_id`` are intentionally optional for the MVP:
    deployments can bind them through config without changing route-gate code.
    """

    profile: str
    model_family: str
    reasoning: str
    role: Literal["route_recommender"] = "route_recommender"
    model_id: str | None = None
    provider_id: str | None = None
    display_label: str | None = None

    def display_name(self) -> str:
        if self.display_label:
            return self.display_label
        if self.reasoning in ("", "provider_default"):
            return self.model_family
        return f"{self.model_family} · {self.reasoning} reasoning"


class RouterConfig(BaseModel):
    primary: RouterProfile
    fallback: RouterProfile


DEFAULT_ROUTER_CONFIG = RouterConfig(
    primary=RouterProfile(
        profile="gpt-5.5-small-reasoning",
        model_family="GPT-5.5",
        reasoning="small",
        model_id="gpt-5.5",
        provider_id="router-profile-catalog",
    ),
    fallback=RouterProfile(
        profile="minimax-m3-routing",
        model_family="MiniMax-M3",
        reasoning="provider_default",
        model_id="minimax-m3",
        provider_id="router-profile-catalog",
    ),
)


def _validate_executable_profile(profile: RouterProfile) -> RouterProfile:
    """Fail loudly when a router profile cannot resolve to a callable model."""
    if not profile.profile.strip():
        raise RuntimeError("Router profile name is required")
    if not profile.model_family.strip():
        raise RuntimeError(f"Router profile {profile.profile!r} is missing model_family")
    if not profile.reasoning.strip():
        raise RuntimeError(f"Router profile {profile.profile!r} is missing reasoning")
    if profile.model_id is None or not profile.model_id.strip():
        raise RuntimeError(f"Router profile {profile.profile!r} is missing model_id")
    if profile.provider_id is None or not profile.provider_id.strip():
        raise RuntimeError(f"Router profile {profile.profile!r} is missing provider_id")
    return profile


def _validate_executable_config(config: RouterConfig) -> RouterConfig:
    _validate_executable_profile(config.primary)
    _validate_executable_profile(config.fallback)
    return config


def router_config() -> RouterConfig:
    """Return executable router profiles, optionally overridden by JSON environment config."""
    raw = os.getenv(_ROUTER_CONFIG_ENV)
    if not raw:
        return _validate_executable_config(DEFAULT_ROUTER_CONFIG)
    try:
        payload = json.loads(raw)
        router_payload = payload.get("router") if isinstance(payload, dict) else None
        return _validate_executable_config(RouterConfig.model_validate(router_payload or payload))
    except (json.JSONDecodeError, ValidationError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"Invalid {_ROUTER_CONFIG_ENV}") from exc


class RouteAlternative(BaseModel):
    harness: str
    model_policy: str
    rationale: str


class RouteProposal(BaseModel):
    proposal_id: str = Field(default_factory=lambda: f"route_{uuid4().hex}")
    created_at: float = Field(default_factory=time.time)
    task_type: str
    recommended_harness: str
    model_policy: str
    model_lane: str
    preferred_model: str | None = None
    reasoning_effort: str
    permission_mode: str
    allowed_billing_classes: list[str] = Field(default_factory=lambda: list(ALLOWED_BILLING_CLASSES))
    forbidden_billing_classes: list[str] = Field(default_factory=lambda: list(FORBIDDEN_BILLING_CLASSES))
    execution_fallback_policy: str = EXECUTION_FALLBACK_POLICY
    alternatives: list[RouteAlternative] = Field(default_factory=list)
    rationale: str
    router_primary_profile: RouterProfile
    router_fallback_profile: RouterProfile
    router_used_profile: RouterProfile
    router_fallback_used: bool
    router_invoked: bool = False
    proposal_source: Literal["default_route_policy", "llm_router"] = "default_route_policy"
    proposal_source_label: str = "Default route policy proposal"
    non_api_billed_constraint: str = "Execution policy: non-API only; API-billed fallback: forbidden"


class RouteDecision(BaseModel):
    proposal_id: str
    action: Literal["accept", "decline", "cancel"]
    user_comment: str | None = None
    final_route: dict[str, Any] | None = None
    router_used_profile: RouterProfile


class RouteProposalStore:
    """Small in-process audit log for route proposal MVP decisions."""

    def __init__(self) -> None:
        self.proposals: dict[str, RouteProposal] = {}
        self.decisions: dict[str, RouteDecision] = {}

    def save_proposal(self, session_id: str, proposal: RouteProposal) -> None:
        self.proposals[f"{session_id}:{proposal.proposal_id}"] = proposal

    def save_decision(self, session_id: str, decision: RouteDecision) -> None:
        self.decisions[f"{session_id}:{decision.proposal_id}"] = decision


route_proposal_store = RouteProposalStore()


def route_approval_gate_enabled() -> bool:
    """Whether Omnigent should pause messages for route approval."""
    raw = os.getenv(_ROUTE_GATE_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _task_type(message: str) -> str:
    lowered = message.lower()
    coding_markers = ("repo", "code", "test", "bug", "implement", "refactor", "file", "typescript", "python")
    if any(marker in lowered for marker in coding_markers):
        return "coding"
    return "general"


def _default_harness(current_harness: str | None, task_type: str) -> str:
    if current_harness:
        return current_harness
    if task_type == "coding":
        return "opencode-native"
    return "pi"


def _rationale(message: str, task_type: str, harness: str) -> str:
    if task_type == "coding":
        return (
            "This looks like a coding/repo task; the selected harness is appropriate, "
            "medium effort is enough for the initial attempt, and API-billed routes are forbidden."
        )
    return (
        "This looks like a general assistant task; the selected harness can handle it under "
        "the non-API-only execution policy."
    )


async def _recommend_with_router(
    *,
    router_profile: RouterProfile,
    config: RouterConfig,
    message: str,
    current_harness: str | None,
    force_fail: bool = False,
) -> RouteProposal:
    """Build a constrained default route policy proposal.

    Router profiles are validated configuration carried for the future LLM
    recommender, but this deterministic MVP does not invoke them yet.
    """
    if force_fail:
        raise RuntimeError(f"router profile unavailable: {router_profile.profile}")
    task_type = _task_type(message)
    harness = _default_harness(current_harness, task_type)
    effort = "medium" if task_type == "coding" else "low"
    model_lane = "coding/default subscription-or-free lane" if task_type == "coding" else "default subscription-or-free lane"
    return RouteProposal(
        task_type=task_type,
        recommended_harness=harness,
        model_policy=model_lane,
        model_lane=model_lane,
        reasoning_effort=effort,
        permission_mode="ask before edits",
        alternatives=[
            RouteAlternative(
                harness="pi",
                model_policy="review/exploration subscription-or-free lane",
                rationale="Useful for read-only exploration or cross-vendor verification.",
            )
        ],
        rationale=_rationale(message, task_type, harness),
        router_primary_profile=config.primary,
        router_fallback_profile=config.fallback,
        router_used_profile=router_profile,
        router_fallback_used=router_profile.profile == config.fallback.profile,
        router_invoked=False,
        proposal_source="default_route_policy",
        proposal_source_label="Default route policy proposal",
    )


async def propose_route(
    message: str,
    *,
    current_harness: str | None,
    config: RouterConfig | None = None,
    force_primary_failure: bool = False,
    force_fallback_failure: bool = False,
) -> RouteProposal:
    """Return a default route policy proposal.

    This MVP does not claim the configured router profile produced the
    recommendation. The primary/fallback chain remains represented and
    validated, but ``router_invoked`` is false until a provider-backed
    recommender is wired.
    """
    resolved_config = config or router_config()
    try:
        return await _recommend_with_router(
            router_profile=resolved_config.primary,
            config=resolved_config,
            message=message,
            current_harness=current_harness,
            force_fail=force_primary_failure,
        )
    except Exception as primary_error:  # noqa: BLE001 - chained fallback must be loud only if all fail
        try:
            return await _recommend_with_router(
                router_profile=resolved_config.fallback,
                config=resolved_config,
                message=message,
                current_harness=current_harness,
                force_fail=force_fallback_failure,
            )
        except Exception as fallback_error:  # noqa: BLE001
            raise RuntimeError(
                f"Routing failed: {resolved_config.primary.profile} failed ({primary_error}); "
                f"{resolved_config.fallback.profile} failed ({fallback_error})"
            ) from fallback_error


def proposal_is_non_api_only(proposal: RouteProposal) -> bool:
    return (
        "api_billed" in proposal.forbidden_billing_classes
        and "unknown" in proposal.forbidden_billing_classes
        and "api_billed" not in proposal.allowed_billing_classes
        and "unknown" not in proposal.allowed_billing_classes
    )


def final_route_from_decision(proposal: RouteProposal, content: dict[str, Any] | None) -> dict[str, Any]:
    final_route = proposal.model_dump(mode="json")
    if not content:
        return final_route
    for key in ("model_policy", "model_lane", "reasoning_effort", "permission_mode"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            final_route[key] = value.strip()
    return final_route
