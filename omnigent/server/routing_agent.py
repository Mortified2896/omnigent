"""LLM-backed model routing agent for native OmniRoute routes."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Literal

import httpx

from omnigent.reasoning_effort import EFFORT_VALUES, validate_effort
from omnigent.server.omniroute_routes import (
    OMNIROUTE_ROUTE_CATALOG,
    get_route_profile,
    reasoning_lte,
)

KNOWN_PERMISSION_MODES = frozenset(
    {
        "ask_before_edits",
        "ask_before_commands",
        "read_only",
        "auto_accept_edits",
        "bypass",
    }
)
KNOWN_HARNESSES = frozenset(
    {
        "opencode-native",
        "open-code-native",
        "OpenCode Native",
        "claude-native",
        "codex-native",
        "pi",
    }
)
_DEFAULT_ROUTE_POLICY_SOURCE = "default_route_policy"
_ROUTER_PROMPT_RULES = (
    "Rules: choose only provided route IDs; do not invent IDs. "
    "Use auto/cheap only for routing/planning/simple chat. "
    "Use auto/coding:cheap or auto/coding:free for light coding. "
    "Use auto/coding for normal coding. "
    "Use auto/coding:pro only for hard coding. "
    "Use auto/reasoning for reasoning-heavy tasks. "
    "Use auto/reasoning:pro only for hardest tasks. "
    "Use auto/vision or auto/multimodal only for multimodal input. "
    "Pick reasoning_effort separately from route ID. "
    "Permission mode is harness safety policy. "
    "API-billed and unknown billing are forbidden unless explicitly allowed. "
    "Use subscriptions when quality matters. "
    "Prefer free only when equivalent."
)
_ROUTER_PROMPT_FIELDS = (
    "Required JSON fields: task_type,recommended_harness,omniroute_route_id,"
    "reasoning_effort,permission_mode,allowed_billing_classes,forbidden_billing_classes,"
    "execution_fallback_policy,omniroute_requires_explicit_approval,rationale "
    "(array of strings)."
)
_ROUTER_PROMPT_EXAMPLE = (
    '{"task_type":"coding","recommended_harness":"OpenCode Native",'
    '"omniroute_route_id":"auto/coding","reasoning_effort":"medium",'
    '"permission_mode":"ask_before_edits",'
    '"allowed_billing_classes":["free","subscription"],'
    '"forbidden_billing_classes":["api_billed","unknown"],'
    '"execution_fallback_policy":"fail_closed_no_api_billed_fallback",'
    '"omniroute_requires_explicit_approval":false,'
    '"rationale":["Normal repository coding task."]}'
)


@dataclass(frozen=True)
class RouteProposal:
    task_type: str
    recommended_harness: str
    omniroute_route_id: str
    reasoning_effort: str
    permission_mode: str
    allowed_billing_classes: list[str]
    forbidden_billing_classes: list[str]
    execution_fallback_policy: str
    omniroute_requires_explicit_approval: bool
    rationale: list[str]
    router_invoked: bool
    router_fallback_used: bool
    proposal_source: Literal["llm_router", "default_route_policy"]
    proposal_source_label: str


@dataclass(frozen=True)
class RouteAlternative:
    omniroute_route_id: str
    reasoning_effort: str
    rationale: str


class RoutingAgentError(RuntimeError):
    pass


def route_approval_gate_enabled() -> bool:
    return os.environ.get("OMNIGENT_ROUTE_APPROVAL_GATE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def validate_route_proposal(proposal: RouteProposal) -> RouteProposal:
    if proposal.proposal_source == "llm_router" and not proposal.router_invoked:
        raise RoutingAgentError("llm_router proposal must have router_invoked=true")
    profile = get_route_profile(proposal.omniroute_route_id)
    if profile is None:
        raise RoutingAgentError(f"unknown OmniRoute route id: {proposal.omniroute_route_id}")
    if proposal.recommended_harness not in KNOWN_HARNESSES:
        raise RoutingAgentError(f"unknown harness: {proposal.recommended_harness}")
    try:
        effort = validate_effort(proposal.reasoning_effort, "route proposal", EFFORT_VALUES)
    except ValueError as exc:
        raise RoutingAgentError(str(exc)) from exc
    if effort not in profile.allowed_reasoning_efforts:
        raise RoutingAgentError(f"reasoning effort {effort!r} not allowed for {profile.route_id}")
    if not reasoning_lte(effort, profile.max_reasoning_effort):
        raise RoutingAgentError(f"reasoning effort {effort!r} exceeds max for {profile.route_id}")
    if proposal.permission_mode not in KNOWN_PERMISSION_MODES:
        raise RoutingAgentError(f"unknown permission mode: {proposal.permission_mode}")
    allowed = set(proposal.allowed_billing_classes)
    if "api_billed" in allowed and not profile.allow_api_billed:
        raise RoutingAgentError(f"api_billed forbidden for {profile.route_id}")
    if "unknown" in allowed and not profile.allow_unknown_billing:
        raise RoutingAgentError(f"unknown billing forbidden for {profile.route_id}")
    if not proposal.rationale:
        raise RoutingAgentError("missing rationale")
    requires = profile.requires_explicit_approval or proposal.omniroute_route_id.endswith(":pro")
    if requires and not proposal.omniroute_requires_explicit_approval:
        proposal = RouteProposal(
            **{**asdict(proposal), "omniroute_requires_explicit_approval": True}
        )
    return proposal


class RoutingAgent:
    def __init__(
        self,
        *,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.primary_model = primary_model or os.environ.get("OMNIGENT_ROUTER_MODEL")
        self.fallback_model = fallback_model or os.environ.get("OMNIGENT_ROUTER_FALLBACK_MODEL")
        self.api_url = api_url or os.environ.get("OMNIGENT_ROUTER_API_URL")
        self.api_key = api_key or os.environ.get("OMNIGENT_ROUTER_API_KEY")
        self.timeout = timeout

    async def propose(
        self,
        *,
        user_message: str,
        available_harnesses: list[str] | None = None,
        billing_policy: str = (
            "free and subscription allowed; "
            "api_billed and unknown forbidden unless explicitly allowed"
        ),
    ) -> RouteProposal:
        errors: list[str] = []
        if self.primary_model and self.api_url:
            try:
                return await self._call_and_validate(
                    self.primary_model, user_message, available_harnesses, billing_policy, False
                )
            except (httpx.HTTPError, RoutingAgentError, ValueError, OSError) as exc:
                errors.append(f"primary: {exc}")
        if self.fallback_model and self.api_url:
            try:
                return await self._call_and_validate(
                    self.fallback_model, user_message, available_harnesses, billing_policy, True
                )
            except (httpx.HTTPError, RoutingAgentError, ValueError, OSError) as exc:
                errors.append(f"fallback: {exc}")
        if os.environ.get("OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY", "").lower() == "true":
            return validate_route_proposal(
                RouteProposal(
                    task_type="coding",
                    recommended_harness="OpenCode Native",
                    omniroute_route_id="auto/coding",
                    reasoning_effort="medium",
                    permission_mode="ask_before_edits",
                    allowed_billing_classes=["free", "subscription"],
                    forbidden_billing_classes=["api_billed", "unknown"],
                    execution_fallback_policy="fail_closed_no_api_billed_fallback",
                    omniroute_requires_explicit_approval=False,
                    rationale=["Router unavailable — default policy used"],
                    router_invoked=False,
                    router_fallback_used=False,
                    proposal_source="default_route_policy",
                    proposal_source_label="Router unavailable — default policy used",
                )
            )
        raise RoutingAgentError(
            "Model Routing Agent unavailable; fail-closed"
            + (": " + "; ".join(errors) if errors else "")
        )

    async def _call_and_validate(
        self,
        model: str,
        user_message: str,
        available_harnesses: list[str] | None,
        billing_policy: str,
        fallback: bool,
    ) -> RouteProposal:
        raw = await self._invoke_router(
            model, self._prompt(user_message, available_harnesses, billing_policy)
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RoutingAgentError("invalid JSON from router") from exc
        if not isinstance(data, dict):
            raise RoutingAgentError("router JSON must be an object")
        data.setdefault("router_invoked", True)
        data.setdefault("router_fallback_used", fallback)
        data.setdefault("proposal_source", "llm_router")
        data.setdefault("proposal_source_label", "Router recommendation")
        proposal = RouteProposal(**data)
        return validate_route_proposal(proposal)

    async def _invoke_router(self, model: str, prompt: str) -> str:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.api_url, headers=headers, json=body)
            resp.raise_for_status()
            payload = resp.json()
        content = payload.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RoutingAgentError("router response missing content")
        return content

    def _prompt(
        self, user_message: str, available_harnesses: list[str] | None, billing_policy: str
    ) -> str:
        routes = [asdict(p) for p in OMNIROUTE_ROUTE_CATALOG.values()]
        return "\n".join(
            [
                "You are Omnigent's Model Routing Agent. Return strict JSON only.",
                f"User message: {user_message}",
                f"Available harnesses: {available_harnesses or ['OpenCode Native']}",
                f"Allowed permission modes: {sorted(KNOWN_PERMISSION_MODES)}",
                f"Billing policy: {billing_policy}",
                f"Available native OmniRoute routes: {json.dumps(routes, separators=(',', ':'))}",
                _ROUTER_PROMPT_RULES,
                _ROUTER_PROMPT_FIELDS,
                f"Example: {_ROUTER_PROMPT_EXAMPLE}",
            ]
        )
