"""LLM-backed model routing agent for native OmniRoute routes."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx

from omnigent.reasoning_effort import EFFORT_VALUES, validate_effort
from omnigent.server.omniroute_routes import (
    NATIVE_OMNIROUTE_ROUTE_IDS,
    OMNIROUTE_ROUTE_CATALOG,
    get_route_profile,
    reasoning_lte,
)

_logger = logging.getLogger(__name__)

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
_KNOWN_BILLING_CLASSES = frozenset({"free", "subscription", "api_billed", "unknown"})
_KNOWN_FALLBACK_POLICIES = frozenset(
    {
        "fail_closed_no_api_billed_fallback",
        "allow_any_fallback",
        "fail_closed",
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


def _build_proposal_json_schema() -> dict[str, object]:
    """Strict JSON schema for :class:`RouteProposal` payloads.

    Used when the routing agent calls a JSON-schema-capable LLM
    (e.g. the server-level :class:`PolicyLLMClient`); the same
    schema is reused by the env-var ``OMNIGENT_ROUTER_*`` direct
    httpx path so the LLM output is constrained to a single object
    type. Mirrors the field list in :data:`_ROUTER_PROMPT_FIELDS`.
    """
    route_enum = sorted(NATIVE_OMNIROUTE_ROUTE_IDS)
    return {
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "description": (
                    "Short category like 'coding', 'general_chat', 'reasoning', 'multimodal'."
                ),
            },
            "recommended_harness": {
                "type": "string",
                "enum": sorted(KNOWN_HARNESSES),
            },
            "omniroute_route_id": {
                "type": "string",
                "enum": route_enum,
            },
            "reasoning_effort": {
                "type": "string",
                "enum": sorted(EFFORT_VALUES),
            },
            "permission_mode": {
                "type": "string",
                "enum": sorted(KNOWN_PERMISSION_MODES),
            },
            "allowed_billing_classes": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_KNOWN_BILLING_CLASSES)},
            },
            "forbidden_billing_classes": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_KNOWN_BILLING_CLASSES)},
            },
            "execution_fallback_policy": {
                "type": "string",
                "enum": sorted(_KNOWN_FALLBACK_POLICIES),
            },
            "omniroute_requires_explicit_approval": {"type": "boolean"},
            "rationale": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
        "required": [
            "task_type",
            "recommended_harness",
            "omniroute_route_id",
            "reasoning_effort",
            "permission_mode",
            "allowed_billing_classes",
            "forbidden_billing_classes",
            "execution_fallback_policy",
            "omniroute_requires_explicit_approval",
            "rationale",
        ],
        "additionalProperties": False,
    }


PROPOSAL_JSON_SCHEMA: dict[str, object] = _build_proposal_json_schema()


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


def fail_open_to_default_policy_enabled() -> bool:
    """Whether the agent falls back to a static default policy on error.

    Disabled by default — fail-closed surfaces a clear user-facing
    error. ``OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY=true``
    (or ``1|yes|on``) opts in. Used only as an emergency escape
    hatch; the default policy is a static hardcoded ``auto/coding``
    proposal and is NOT a router recommendation.
    """
    return os.environ.get("OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
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
    """Model Routing Agent — picks a native OmniRoute for an inbound user message.

    Resolution order:

    1. **Server-level LLM client** (``policy_llm_client``): the same
       :class:`PolicyLLMClient` used for policy functions / smart
       routing. Preferred path on this machine since it inherits
       the existing ``llm:`` server config wiring.
    2. **Direct httpx** using the ``OMNIGENT_ROUTER_*`` env vars.
       Useful when the routing agent should target an explicit
       endpoint (e.g. a smaller / cheaper model split off from
       the main policy LLM).
    3. **Fail-closed** by default: raises :class:`RoutingAgentError`
       with a clear ``"unavailable; fail-closed"`` message. Set
       ``OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY=true`` to
       opt back into the emergency default policy (NOT a router
       recommendation — labelled ``"Router unavailable — default
       policy used"``).
    """

    def __init__(
        self,
        *,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
        policy_llm_client: Any | None = None,
    ) -> None:
        self.primary_model = primary_model or os.environ.get("OMNIGENT_ROUTER_MODEL")
        self.fallback_model = fallback_model or os.environ.get("OMNIGENT_ROUTER_FALLBACK_MODEL")
        self.api_url = api_url or os.environ.get("OMNIGENT_ROUTER_API_URL")
        self.api_key = api_key or os.environ.get("OMNIGENT_ROUTER_API_KEY")
        self.timeout = timeout
        self.policy_llm_client = policy_llm_client

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
        if self.policy_llm_client is not None:
            try:
                return await self._call_via_policy_llm(
                    user_message, available_harnesses, billing_policy, False
                )
            except (
                httpx.HTTPError,
                RoutingAgentError,
                ValueError,
                OSError,
                AttributeError,
            ) as exc:
                errors.append(f"policy_llm: {exc}")
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
        if fail_open_to_default_policy_enabled():
            _logger.warning(
                "RoutingAgent: no router reachable; falling back to default policy "
                "(OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY=true)"
            )
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
        data = self._parse_router_response(raw)
        data.setdefault("router_invoked", True)
        data.setdefault("router_fallback_used", fallback)
        data.setdefault("proposal_source", "llm_router")
        data.setdefault("proposal_source_label", "Router recommendation")
        proposal = RouteProposal(**data)
        return validate_route_proposal(proposal)

    async def _call_via_policy_llm(
        self,
        user_message: str,
        available_harnesses: list[str] | None,
        billing_policy: str,
        fallback: bool,
    ) -> RouteProposal:
        """Call the server-level :class:`PolicyLLMClient` for a JSON proposal."""
        prompt = self._prompt(user_message, available_harnesses, billing_policy)
        response = await self.policy_llm_client.create(
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            instructions=(
                "You are Omnigent's Model Routing Agent. Return strict JSON matching "
                "the provided schema only; never add markdown, prose, or extra keys."
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "route_proposal",
                    "strict": True,
                    "schema": PROPOSAL_JSON_SCHEMA,
                }
            },
        )
        try:
            text = response.output[0].content[0].text
        except (AttributeError, IndexError, TypeError) as exc:
            raise RoutingAgentError("router response missing content") from exc
        if not isinstance(text, str) or not text.strip():
            raise RoutingAgentError("router response missing content")
        data = self._parse_router_response(text)
        data.setdefault("router_invoked", True)
        data.setdefault("router_fallback_used", fallback)
        data.setdefault("proposal_source", "llm_router")
        data.setdefault("proposal_source_label", "Router recommendation")
        proposal = RouteProposal(**data)
        return validate_route_proposal(proposal)

    @staticmethod
    def _parse_router_response(raw: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RoutingAgentError("invalid JSON from router") from exc
        if not isinstance(data, dict):
            raise RoutingAgentError("router JSON must be an object")
        return data

    async def _invoke_router(self, model: str, prompt: str) -> str:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": prompt}],
            "temperature": 0,
            # Local OmniRoute returns SSE by default; force a single JSON
            # body so ``resp.json()`` works without consuming a stream.
            "stream": False,
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


def build_routing_agent_from_runtime() -> RoutingAgent:
    """Build a :class:`RoutingAgent` using the active runtime configuration.

    Preferred path: pass a :class:`PolicyLLMClient` constructed from
    ``RuntimeCaps.llm`` so the routing agent reuses the server-level
    LLM (the same wiring :mod:`omnigent.server.smart_routing` uses).
    Falls back to env-var httpx when no server-level LLM is set.

    Connection resolution honours ``policy_llm_connection_factory``
    (managed deployments that bill per-caller) before falling back
    to the static ``llm.connection`` / ``llm.profile`` path used by
    the policy engine.

    Never raises — returns a RoutingAgent that may fail-closed when
    invoked; callers (``_await_route_approval``) translate that to
    the user-facing error. The returned agent always has a config;
    no caller should ever instantiate ``RoutingAgent()`` with empty
    kwargs again, since that silently routes to ``httpx`` env vars
    the server does not necessarily have.
    """
    try:
        from omnigent.runtime._globals import get_caps
        from omnigent.runtime.policies.builder import (
            _build_policy_llm_client,
            _resolve_server_llm_connection,
        )
    except ImportError:
        get_caps = None  # type: ignore[assignment]

    policy_client: Any | None = None
    if get_caps is not None:
        try:
            caps = get_caps()
        except Exception:  # noqa: BLE001  # never propagate
            caps = None
        server_llm = getattr(caps, "llm", None) if caps is not None else None
        if server_llm is not None:
            connection: dict[str, str] | None = None
            factory = (
                getattr(caps, "policy_llm_connection_factory", None) if caps is not None else None
            )
            if callable(factory):
                try:
                    connection = factory()
                except Exception:  # noqa: BLE001
                    connection = None
            if not connection:
                try:
                    connection = _resolve_server_llm_connection(server_llm)
                except Exception:  # noqa: BLE001
                    connection = None
            policy_client = _build_policy_llm_client(server_llm, connection)

    return RoutingAgent(policy_llm_client=policy_client)
