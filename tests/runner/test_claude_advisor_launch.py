"""Claude native launch keeps the frozen route, account and model together."""

import json

import pytest

from omnigent.model_advisor_binding import encode_transport_binding_label
from omnigent.model_advisor_provider_policy import LogicalChoice
from omnigent.runner.native.orchestration import (
    _claude_launch_metadata_from_envelope,
    _ClaudeSessionLaunchMetadata,
    _validate_claude_advisor_launch,
)
from omnigent.runner.session_init_protocol import (
    SESSION_INIT_PROTOCOL_VERSION,
    RunnerSessionInitEnvelope,
)


def metadata():
    choice = LogicalChoice("anthropic", "claude-test", "high")
    route = {
        "choice": {
            "provider": choice.provider,
            "model_id": choice.model_id,
            "reasoning_effort": choice.reasoning_effort,
        },
        "transport": "direct",
        "route_id": "claude-direct",
        "wire_model": choice.model_id,
        "wire_effort": "high",
        "entitlement_kind": "claude_plan",
        "entitlement_key": "claude-plan:account",
        "equivalence_key": "anthropic:claude-native:responses:claude-test:high",
        "catalog_revision": "host-catalog",
        "ready": True,
        "connection_id": "claude-login",
    }
    plan = {
        "choice": route["choice"],
        "preference": "direct_only",
        "primary": route,
        "fallback": None,
        "reason": "direct_only",
    }
    labels = {
        "omnigent.advisor.round_id": "round-test",
        "omnigent.advisor.logical_choice_id": choice.choice_id,
        "omnigent.advisor.connection_id": "claude-login",
        "omnigent.access_lane": "claude-direct",
        **encode_transport_binding_label("omnigent.advisor.dispatch_route", json.dumps(route)),
        **encode_transport_binding_label("omnigent.advisor.transport_plan", json.dumps(plan)),
    }
    return _ClaudeSessionLaunchMetadata(
        model_override=choice.model_id, reasoning_effort="high", labels=labels
    )


def test_session_envelope_preserves_chunked_advisor_labels():
    original = metadata()
    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id="session",
        agent_id="agent",
        snapshot={
            "created_at": 0,
            "updated_at": 0,
            "model_override": original.model_override,
            "reasoning_effort": original.reasoning_effort,
            "labels": original.labels,
        },
    )
    loaded = _claude_launch_metadata_from_envelope(envelope)
    assert loaded.labels == original.labels
    assert loaded.model_override == original.model_override


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", [None, "account", "gateway", "config", "resolver", "model", "connection"]
)
async def test_claude_launch_rechecks_frozen_subscription_binding(monkeypatch, changed):
    launch = metadata()
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.claude_subscription_identity",
        lambda: "claude-plan:other" if changed == "account" else "claude-plan:account",
    )
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.managed_claude_gateway_signal",
        lambda: ("https://gateway.test" if changed == "gateway" else None, False),
    )
    if changed == "model":
        launch = _ClaudeSessionLaunchMetadata(
            model_override="claude-other", reasoning_effort="high", labels=launch.labels
        )
    if changed == "connection":
        launch.labels["omnigent.advisor.connection_id"] = "codex-login"
    kwargs = {
        "session_id": "session",
        "provider_configured": changed == "config",
        "resolution_failed": changed == "resolver",
    }
    if changed:
        with pytest.raises(RuntimeError):
            await _validate_claude_advisor_launch(launch, **kwargs)
    else:
        await _validate_claude_advisor_launch(launch, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", [None, "account", "connection", "model", "resolver", "missing"]
)
async def test_codex_backed_claude_launch_rechecks_the_live_bridge(monkeypatch, changed):
    from omnigent.model_advisor_provider_policy import QualifiedRoute, plan_transport

    choice = LogicalChoice("openai", "fixture-checkpoint", "low", "claude-native")
    route = QualifiedRoute(
        choice=choice,
        transport="direct",
        route_id="claude-codex",
        wire_model=choice.model_id,
        wire_effort="low",
        entitlement_kind="chatgpt_plan",
        entitlement_key="chatgpt-plan:bridge:account",
        equivalence_key="fixture-claude-contract",
        catalog_revision="fixture",
        ready=True,
        connection_id="codex-claude-bridge:connection",
    )
    plan = plan_transport(choice, "direct_only", (route,))
    labels = {
        "omnigent.advisor.round_id": "fixture-round",
        "omnigent.advisor.logical_choice_id": choice.choice_id,
        "omnigent.advisor.connection_id": route.connection_id,
        "omnigent.access_lane": route.route_id,
        **encode_transport_binding_label(
            "omnigent.advisor.dispatch_route", json.dumps(route.to_payload())
        ),
        **encode_transport_binding_label(
            "omnigent.advisor.transport_plan", json.dumps(plan.to_payload())
        ),
    }
    launch = _ClaudeSessionLaunchMetadata(
        model_override=choice.model_id, reasoning_effort="low", labels=labels
    )

    async def identity(_config):
        if changed == "missing":
            return None
        return {
            "entitlement_key": "chatgpt-plan:bridge:other"
            if changed == "account"
            else route.entitlement_key,
            "connection_id": "codex-claude-bridge:other"
            if changed == "connection"
            else route.connection_id,
            "models": [{"model": "other" if changed == "model" else choice.model_id}],
        }

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.codex_subscription.subscription_identity", identity
    )
    kwargs = {
        "session_id": "fixture-session",
        "provider_configured": True,
        "resolution_failed": changed == "resolver",
        "claude_config": object(),
    }
    if changed:
        with pytest.raises(RuntimeError, match="binding changed"):
            await _validate_claude_advisor_launch(launch, **kwargs)
    else:
        await _validate_claude_advisor_launch(launch, **kwargs)
