import json

import pytest

from omnigent.server.routing_agent import (
    RouteProposal,
    RoutingAgent,
    RoutingAgentError,
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
