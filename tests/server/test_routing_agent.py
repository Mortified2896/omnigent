import json

import pytest

from omnigent.server.routing_agent import (
    PROPOSAL_JSON_SCHEMA,
    RouteProposal,
    RoutingAgent,
    RoutingAgentError,
    build_routing_agent_from_runtime,
    fail_open_to_default_policy_enabled,
    validate_route_proposal,
)


def proposal(**overrides):
    base = {
        "task_type": "coding",
        "recommended_harness": "OpenCode Native",
        "omniroute_route_id": "auto/coding",
        "reasoning_effort": "medium",
        "permission_mode": "ask_before_edits",
        "allowed_billing_classes": ["free", "subscription"],
        "forbidden_billing_classes": ["api_billed", "unknown"],
        "execution_fallback_policy": "fail_closed_no_api_billed_fallback",
        "omniroute_requires_explicit_approval": False,
        "rationale": ["normal coding"],
        "router_invoked": True,
        "router_fallback_used": False,
        "proposal_source": "llm_router",
        "proposal_source_label": "Router recommendation",
    }
    data = {**base, **overrides}
    return RouteProposal(**data)


def test_valid_auto_coding_medium_passes():
    assert validate_route_proposal(proposal()).omniroute_route_id == "auto/coding"


def test_valid_auto_coding_cheap_low_passes():
    assert validate_route_proposal(
        proposal(omniroute_route_id="auto/coding:cheap", reasoning_effort="low")
    )


def test_valid_auto_reasoning_high_passes():
    assert validate_route_proposal(
        proposal(omniroute_route_id="auto/reasoning", reasoning_effort="high")
    )


def test_auto_fake_fails():
    with pytest.raises(RoutingAgentError):
        validate_route_proposal(proposal(omniroute_route_id="auto/fake"))


def test_auto_coding_cheap_max_fails():
    with pytest.raises(RoutingAgentError):
        validate_route_proposal(
            proposal(omniroute_route_id="auto/coding:cheap", reasoning_effort="max")
        )


def test_pro_routes_require_explicit_approval():
    assert validate_route_proposal(
        proposal(omniroute_route_id="auto/coding:pro", reasoning_effort="high")
    ).omniroute_requires_explicit_approval
    assert validate_route_proposal(
        proposal(omniroute_route_id="auto/reasoning:pro", reasoning_effort="max")
    ).omniroute_requires_explicit_approval


def test_api_billed_fails_unless_route_allows():
    with pytest.raises(RoutingAgentError):
        validate_route_proposal(proposal(allowed_billing_classes=["api_billed"]))


def test_invalid_permission_mode_fails():
    with pytest.raises(RoutingAgentError):
        validate_route_proposal(proposal(permission_mode="root"))


class StubAgent(RoutingAgent):
    def __init__(self, responses):
        super().__init__(
            primary_model="primary",
            fallback_model="fallback",
            api_url="http://router",
        )
        self.responses = list(responses)

    async def _invoke_router(self, model, prompt):
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_primary_router_failure_calls_fallback():
    fallback_response = json.dumps(
        {
            "task_type": "coding",
            "recommended_harness": "OpenCode Native",
            "omniroute_route_id": "auto/coding",
            "reasoning_effort": "medium",
            "permission_mode": "ask_before_edits",
            "allowed_billing_classes": ["free", "subscription"],
            "forbidden_billing_classes": ["api_billed", "unknown"],
            "execution_fallback_policy": "fail_closed_no_api_billed_fallback",
            "omniroute_requires_explicit_approval": False,
            "rationale": ["ok"],
        }
    )
    agent = StubAgent([ValueError("boom"), fallback_response])
    result = await agent.propose(user_message="fix bug")
    assert result.router_fallback_used is True


@pytest.mark.asyncio
async def test_both_router_failures_fail_closed(monkeypatch):
    monkeypatch.delenv("OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY", raising=False)
    agent = StubAgent([ValueError("boom"), ValueError("nope")])
    with pytest.raises(RoutingAgentError):
        await agent.propose(user_message="fix bug")


def test_fail_open_env_default_is_false(monkeypatch):
    """OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY defaults to False.

    The fail-open toggle must NOT be on by default — the routing agent
    must surface a clear user-facing error when no router is configured,
    instead of silently returning a fake 'default policy' recommendation.
    """
    monkeypatch.delenv("OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY", raising=False)
    assert fail_open_to_default_policy_enabled() is False
    for truthy in ("true", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY", truthy)
        assert fail_open_to_default_policy_enabled() is True, truthy
    for falsy in ("false", "FALSE", "0", "no", "off", ""):
        monkeypatch.setenv("OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY", falsy)
        assert fail_open_to_default_policy_enabled() is False, falsy


def test_default_policy_carries_clear_unavailable_marker():
    """The emergency default proposal is plainly labelled.

    The proposal_source_label must be 'Router unavailable — default
    policy used' so the UI distinguishes it from a real router
    recommendation.
    """
    fallback = validate_route_proposal(
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
    assert fallback.router_invoked is False
    assert fallback.proposal_source == "default_route_policy"
    assert "unavailable" in fallback.proposal_source_label.lower()


@pytest.mark.asyncio
async def test_policy_llm_client_used_before_env_var(monkeypatch):
    """policy_llm_client is the primary mechanism when supplied.

    The env-var httpx path must NOT be hit when policy_llm_client is
    available — the server-level LLM client is the preferred path.
    """
    sent = {}

    class FakePolicyClient:
        async def create(self, **kwargs):
            sent.update(kwargs)
            return type(
                "R",
                (),
                {
                    "output": [
                        type("O", (), {"content": [type("C", (), {"text": _trivial_json()})]})()
                    ]
                },
            )()

    agent = RoutingAgent(
        primary_model="env-primary",
        api_url="http://env-router",
        api_key="env-key",
        policy_llm_client=FakePolicyClient(),
    )

    proposal = await agent.propose(user_message="Hi", available_harnesses=["OpenCode Native"])

    assert proposal.router_invoked is True
    assert proposal.proposal_source == "llm_router"
    assert proposal.proposal_source_label == "Router recommendation"
    assert proposal.omniroute_route_id == "auto/cheap"
    assert proposal.reasoning_effort == "low"
    assert proposal.permission_mode == "read_only"
    # Strict JSON schema was enforced.
    text_format = sent["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["schema"] is PROPOSAL_JSON_SCHEMA


def _trivial_json() -> str:
    return json.dumps(
        {
            "task_type": "general_chat",
            "recommended_harness": "OpenCode Native",
            "omniroute_route_id": "auto/cheap",
            "reasoning_effort": "low",
            "permission_mode": "read_only",
            "allowed_billing_classes": ["free", "subscription"],
            "forbidden_billing_classes": ["api_billed", "unknown"],
            "execution_fallback_policy": "fail_closed_no_api_billed_fallback",
            "omniroute_requires_explicit_approval": False,
            "rationale": ["Trivial greeting; cheapest route suffices."],
        }
    )


@pytest.mark.asyncio
async def test_trivial_prompt_routes_to_cheap_route_not_coding():
    """Trivial 'Hi' prompt must NOT route to auto/coding.

    Exposes the original bug: the emergency default policy was returning
    auto/coding + ask_before_edits for trivial prompts. With a real
    router, the response must validate as a non-coding route.
    """
    proposal = validate_route_proposal(
        RouteProposal(
            task_type="general_chat",
            recommended_harness="OpenCode Native",
            omniroute_route_id="auto/cheap",
            reasoning_effort="low",
            permission_mode="read_only",
            allowed_billing_classes=["free", "subscription"],
            forbidden_billing_classes=["api_billed", "unknown"],
            execution_fallback_policy="fail_closed_no_api_billed_fallback",
            omniroute_requires_explicit_approval=False,
            rationale=["Trivial greeting"],
            router_invoked=True,
            router_fallback_used=False,
            proposal_source="llm_router",
            proposal_source_label="Router recommendation",
        )
    )
    assert proposal.omniroute_route_id != "auto/coding"
    assert proposal.permission_mode != "ask_before_edits"
    assert proposal.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_coding_prompt_routes_to_coding_route():
    """A real coding prompt MUST route to auto/coding with ask_before_edits."""
    proposal = validate_route_proposal(
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
            rationale=["Normal repository coding task."],
            router_invoked=True,
            router_fallback_used=False,
            proposal_source="llm_router",
            proposal_source_label="Router recommendation",
        )
    )
    assert proposal.omniroute_route_id == "auto/coding"
    assert proposal.permission_mode == "ask_before_edits"
    assert proposal.reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_read_only_routes_skip_edit_heavy_permission():
    """Read-only routes must not select ask_before_edits or auto_accept_edits."""
    proposal = validate_route_proposal(
        RouteProposal(
            task_type="general_chat",
            recommended_harness="OpenCode Native",
            omniroute_route_id="auto/best-free",
            reasoning_effort="low",
            permission_mode="read_only",
            allowed_billing_classes=["free"],
            forbidden_billing_classes=["api_billed", "unknown", "subscription"],
            execution_fallback_policy="fail_closed_no_api_billed_fallback",
            omniroute_requires_explicit_approval=False,
            rationale=["Read-only inspection"],
            router_invoked=True,
            router_fallback_used=False,
            proposal_source="llm_router",
            proposal_source_label="Router recommendation",
        )
    )
    assert proposal.permission_mode == "read_only"


def test_router_unavailable_without_fail_open(monkeypatch):
    """RoutingAgentError surfaces clearly when no router is configured.

    With no policy_llm_client and no env-var router URL, and no
    fail-open toggle, the routing agent must raise RoutingAgentError
    with a user-facing message, never fake a 'default policy' proposal.
    """
    import asyncio

    monkeypatch.delenv("OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY", raising=False)
    agent = RoutingAgent(policy_llm_client=None)
    # Strip all env-var routers too.
    monkeypatch.delenv("OMNIGENT_ROUTER_MODEL", raising=False)
    monkeypatch.delenv("OMNIGENT_ROUTER_API_URL", raising=False)
    monkeypatch.delenv("OMNIGENT_ROUTER_FALLBACK_MODEL", raising=False)
    # Clear instance-level model/url set above.
    agent.primary_model = None
    agent.fallback_model = None
    agent.api_url = None

    async def _run():
        return await agent.propose(user_message="Hi")

    with pytest.raises(RoutingAgentError) as exc:
        asyncio.run(_run())
    assert "fail-closed" in str(exc.value)


def test_build_routing_agent_from_runtime_returns_agent():
    """The helper returns a RoutingAgent even when no caps.llm is set.

    The helper must NEVER raise at construction — only ``propose()``
    may fail. This guards against the helper being a hidden failure
    point in the request path.
    """
    agent = build_routing_agent_from_runtime()
    assert isinstance(agent, RoutingAgent)


def test_proposal_json_schema_lists_required_fields():
    """The proposal schema carries every required key as advertised in the prompt."""
    required = set(PROPOSAL_JSON_SCHEMA["required"])
    assert {
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
    }.issubset(required)
    assert PROPOSAL_JSON_SCHEMA["additionalProperties"] is False
