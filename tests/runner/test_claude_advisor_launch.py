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
