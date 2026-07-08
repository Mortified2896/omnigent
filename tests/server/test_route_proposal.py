import json

import pytest

from omnigent.server.route_proposal import (
    DEFAULT_ROUTER_CONFIG,
    final_route_from_decision,
    proposal_is_non_api_only,
    propose_route,
    router_config,
)


@pytest.mark.asyncio
async def test_primary_router_succeeds_without_fallback():
    proposal = await propose_route("Implement tests for this repo", current_harness="codex-native")

    assert proposal.router_used_profile.profile == DEFAULT_ROUTER_CONFIG.primary.profile
    assert proposal.router_used_profile.model_family == "GPT-5.5"
    assert proposal.router_used_profile.reasoning == "small"
    assert proposal.router_used_profile.model_id == "gpt-5.5"
    assert proposal.router_used_profile.provider_id == "router-profile-catalog"
    assert proposal.router_fallback_used is False
    assert proposal.router_invoked is False
    assert proposal.proposal_source == "default_route_policy"
    assert proposal.recommended_harness == "codex-native"
    assert proposal_is_non_api_only(proposal)


@pytest.mark.asyncio
async def test_primary_router_fails_fallback_used():
    proposal = await propose_route(
        "Implement a backend route gate",
        current_harness="claude-native",
        force_primary_failure=True,
    )

    assert proposal.router_used_profile.profile == DEFAULT_ROUTER_CONFIG.fallback.profile
    assert proposal.router_used_profile.model_family == "MiniMax-M3"
    assert proposal.router_used_profile.reasoning == "provider_default"
    assert proposal.router_used_profile.model_id == "minimax-m3"
    assert proposal.router_used_profile.provider_id == "router-profile-catalog"
    assert proposal.router_fallback_used is True
    assert proposal.router_invoked is False


def test_route_approval_gate_can_be_disabled(monkeypatch):
    from omnigent.server.route_proposal import route_approval_gate_enabled

    monkeypatch.setenv("OMNIGENT_ROUTE_APPROVAL_GATE", "0")

    assert route_approval_gate_enabled() is False


@pytest.mark.asyncio
async def test_both_routers_fail_loudly():
    with pytest.raises(RuntimeError, match="Routing failed"):
        await propose_route(
            "Implement a backend route gate",
            current_harness="claude-native",
            force_primary_failure=True,
            force_fallback_failure=True,
        )


def test_router_profiles_can_come_from_config(monkeypatch):
    monkeypatch.setenv(
        "OMNIGENT_ROUTER_PROFILE_CONFIG",
        json.dumps(
            {
                "router": {
                    "primary": {
                        "profile": "custom-primary",
                        "model_family": "custom-family",
                        "reasoning": "tiny",
                        "role": "route_recommender",
                        "model_id": "provider/custom-model",
                        "provider_id": "provider",
                    },
                    "fallback": {
                        "profile": "custom-fallback",
                        "model_family": "fallback-family",
                        "reasoning": "provider_default",
                        "role": "route_recommender",
                        "model_id": "provider/fallback-model",
                        "provider_id": "provider",
                    },
                }
            }
        ),
    )

    config = router_config()

    assert config.primary.profile == "custom-primary"
    assert config.primary.model_id == "provider/custom-model"
    assert config.primary.reasoning == "tiny"


def test_router_profile_without_model_id_fails_loudly(monkeypatch):
    monkeypatch.setenv(
        "OMNIGENT_ROUTER_PROFILE_CONFIG",
        json.dumps(
            {
                "router": {
                    "primary": {
                        "profile": "custom-primary",
                        "model_family": "custom-family",
                        "reasoning": "tiny",
                        "role": "route_recommender",
                        "provider_id": "provider",
                    },
                    "fallback": {
                        "profile": "custom-fallback",
                        "model_family": "fallback-family",
                        "reasoning": "provider_default",
                        "role": "route_recommender",
                        "model_id": "provider/fallback-model",
                        "provider_id": "provider",
                    },
                }
            }
        ),
    )

    with pytest.raises(RuntimeError, match="missing model_id"):
        router_config()


@pytest.mark.asyncio
async def test_modify_approved_route_overrides_fields():
    proposal = await propose_route("Implement a frontend card", current_harness="pi")

    final_route = final_route_from_decision(
        proposal,
        {
            "model_lane": "review/exploration subscription-or-free lane",
            "reasoning_effort": "high",
            "permission_mode": "read-only",
            "comment": "please be careful",
        },
    )

    assert final_route["model_lane"] == "review/exploration subscription-or-free lane"
    assert final_route["reasoning_effort"] == "high"
    assert final_route["permission_mode"] == "read-only"
    assert final_route["forbidden_billing_classes"] == ["api_billed", "unknown"]
    assert final_route["router_used_profile"]["profile"] == DEFAULT_ROUTER_CONFIG.primary.profile
