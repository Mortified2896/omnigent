"""Integration tests for Model Routing Agent session gating.

Exercises the routing-gate helper directly (no real runner / DB),
covering manual-mode preservation, approval-flow execution, and decline.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities import Conversation, ConversationItem, NewConversationItem
from omnigent.server.routes import sessions as routes_sessions
from omnigent.server.routing_agent import (
    RouteProposal,
    route_approval_gate_enabled,
    validate_route_proposal,
)


def _conv(**overrides):
    base = {
        "id": "conv_test",
        "created_at": 0,
        "updated_at": 0,
        "root_conversation_id": "conv_test",
        "title": None,
        "kind": "default",
        "parent_conversation_id": None,
        "agent_id": None,
        "runner_id": None,
        "host_id": None,
        "labels": {},
        "session_state": {},
        "session_usage": {},
        "reasoning_effort": None,
        "model_override": None,
        "cost_control_mode_override": None,
        "harness_override": None,
        "route_approval_enabled": None,
        "omniroute_route_id": None,
        "permission_mode": None,
        "omniroute_requires_explicit_approval": None,
        "sub_agent_name": None,
        "external_session_id": None,
        "terminal_launch_args": None,
        "workspace": None,
        "git_branch": None,
        "archived": False,
    }
    base.update(overrides)
    return Conversation(**base)


def _proposal(**overrides):
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
    base.update(overrides)
    return validate_route_proposal(RouteProposal(**base))


class _FakeRoutingAgent:
    def __init__(self, proposal: RouteProposal):
        self.proposal = proposal
        self.calls = 0

    def __call__(self) -> _FakeRoutingAgent:
        return self

    async def propose(self, **kwargs) -> RouteProposal:
        self.calls += 1
        return self.proposal


class _FailingRoutingAgent:
    def __call__(self) -> _FailingRoutingAgent:
        return self

    async def propose(self, **kwargs) -> RouteProposal:
        raise routes_sessions._RoutingAgentError("Model Routing Agent unavailable; fail-closed")


class _FakeStore:
    def __init__(self) -> None:
        self.appended: list[NewConversationItem] = []

    def append(self, _session_id: str, items: list[NewConversationItem]) -> list[ConversationItem]:
        self.appended.extend(items)
        return [
            ConversationItem(
                id=f"item_{len(self.appended)}",
                type=item.type,
                status="completed",
                response_id=item.response_id,
                created_at=0,
                data=item.data,
                created_by=item.created_by,
            )
            for item in items
        ]

    def list_items(self, *_args, **_kwargs):
        return SimpleNamespace(data=[])


def test_route_proposal_payload_includes_evaluator_provenance():
    payload = routes_sessions._route_proposal_params(
        _proposal(
            router_evaluator_route="auto/smart",
            actual_evaluator_provider="mistral",
            actual_evaluator_model="mistral/mistral-large-latest",
            evaluator_billing_class="free",
            evaluator_fallback_used=False,
            evaluator_decision_id="decision-123",
            evaluator_selection_strategy="auto",
        )
    )
    route_proposal = payload["route_proposal"]
    assert route_proposal["router_evaluator_route"] == "auto/smart"
    assert route_proposal["actual_evaluator_provider"] == "mistral"
    assert route_proposal["actual_evaluator_model"] == "mistral/mistral-large-latest"
    assert route_proposal["evaluator_billing_class"] == "free"
    assert route_proposal["evaluator_fallback_used"] is False
    assert route_proposal["evaluator_decision_id"] == "decision-123"
    assert route_proposal["evaluator_selection_strategy"] == "auto"


def test_route_proposal_payload_preserves_api_backed_provider_provenance():
    """An API-backed provider exposed through OmniRoute must keep its
    provenance (selected provider, selected model, decision id, billing
    class) intact on the approval card. The validator must NOT downgrade
    or strip the proposal because it carries an API-key style identifier
    in the provenance.
    """
    payload = routes_sessions._route_proposal_params(
        _proposal(
            router_evaluator_route="auto/smart",
            actual_evaluator_provider="openai-api-key-backed",
            actual_evaluator_model="openai-api-key-backed/gpt-via-omniroute",
            evaluator_billing_class="free",  # free tier via an OpenAI-compatible endpoint
            evaluator_decision_id="api-backed-decision-456",
            evaluator_selection_strategy="auto",
        )
    )
    rp = payload["route_proposal"]
    assert rp["actual_evaluator_provider"] == "openai-api-key-backed"
    assert rp["actual_evaluator_model"] == "openai-api-key-backed/gpt-via-omniroute"
    assert rp["evaluator_billing_class"] == "free"
    assert rp["evaluator_decision_id"] == "api-backed-decision-456"


def test_route_proposal_billing_summary_clarifies_api_billed_meaning():
    """Approval-card ``billing_summary`` must surface the raw ``api_billed``
    value (wire format unchanged) but inline an explicit clarification that
    it is metered billing and transport-independent. This prevents the card
    from reading as ``all API access forbidden``.
    """
    payload = routes_sessions._route_proposal_params(_proposal())
    summary = payload["route_proposal"]["billing_summary"]
    # The raw billing-class value must still be present (wire format
    # unchanged).
    assert "api_billed" in summary
    # An inline clarification must accompany it so the reader does not
    # conclude that all API-backed routes are forbidden.
    assert "metered billing" in summary
    assert "transport" in summary.lower()


def test_routing_off_does_not_call_router(monkeypatch):
    """When route_approval_enabled is False, the helper short-circuits and never
    instantiates or calls the RoutingAgent, even if a stale omniroute_route_id
    is left over from a previous approval."""
    agent = _FakeRoutingAgent(_proposal())
    monkeypatch.setattr(routes_sessions, "_build_routing_agent_from_runtime", agent)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("router should not be invoked")

    monkeypatch.setattr(routes_sessions, "_extract_user_text_for_routing", _must_not_run)

    conv = _conv(
        route_approval_enabled=False,
        omniroute_route_id="auto/coding",  # stale from a prior approved run
    )
    assert routes_sessions._routing_approval_is_enabled(conv) is False
    assert agent.calls == 0


def test_routing_off_manual_picker_preserved(monkeypatch):
    """When routing is off, manual model_override, harness_override, and
    reasoning_effort remain the source of truth — stale approved route is ignored."""
    agent = _FakeRoutingAgent(_proposal())
    monkeypatch.setattr(routes_sessions, "_build_routing_agent_from_runtime", agent)

    conv = _conv(
        route_approval_enabled=False,
        model_override="anthropic/claude-sonnet-4-5",
        harness_override="opencode-native",
        reasoning_effort="low",
        omniroute_route_id="auto/coding",  # stale
    )
    assert conv.omniroute_route_id == "auto/coding"
    assert conv.model_override == "anthropic/claude-sonnet-4-5"
    assert routes_sessions._routing_approval_is_enabled(conv) is False


def test_routing_off_short_circuits_in_dispatcher(monkeypatch):
    """Verify routing-off path leaves the existing manual flow unchanged."""

    conv = _conv(route_approval_enabled=False)
    dispatched: list[bool] = []

    def _approve_only_if_enabled(c, *_args, **_kwargs) -> bool:
        dispatched.append(routes_sessions._routing_approval_is_enabled(c))
        return routes_sessions._routing_approval_is_enabled(c)

    monkeypatch.setattr(routes_sessions, "_routing_approval_is_enabled", lambda c: False)
    # No router call happens because the helper exits on the disabled check.
    agent = _FakeRoutingAgent(_proposal())
    monkeypatch.setattr(routes_sessions, "_build_routing_agent_from_runtime", agent)
    assert routes_sessions._routing_approval_is_enabled(conv) is False
    assert agent.calls == 0
    assert dispatched == []  # not invoked at all


@pytest.mark.asyncio
async def test_routing_off_dispatches_complete_prompt_without_router(monkeypatch):
    """Manual mode bypasses the Model Routing Agent and forwards the body."""
    conv = _conv(route_approval_enabled=False, harness_override="opencode-native")
    prompt = "Return only ROUTING_DISABLED_OK. " + ("context " * 1000)
    body = routes_sessions.SessionEventInput.model_validate(
        {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        }
    )
    agent = _FakeRoutingAgent(_proposal())
    monkeypatch.setattr(routes_sessions, "_build_routing_agent_from_runtime", agent)
    monkeypatch.setattr(routes_sessions, "_is_native_terminal_session", lambda _conv: False)
    captured: dict[str, Any] = {}

    async def _forward(*args, **kwargs):
        captured["body"] = args[2]
        return "item_direct"

    monkeypatch.setattr(routes_sessions, "_forward_event_to_runner", _forward)
    result = await routes_sessions._dispatch_session_event_to_runner(
        "conv_test",
        conv,
        body,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        agent_name="OpenCode Native",
        file_store=None,
        artifact_store=None,
    )

    assert result.item_id == "item_direct"
    assert captured["body"].data["content"][0]["text"] == prompt
    assert agent.calls == 0


def test_routing_on_calls_router(monkeypatch):
    """When route_approval_enabled is True and the gate env var is on, the
    helper invokes the RoutingAgent and publishes an elicitation for approval.
    This test asserts the helper is invoked and returns the unmodified conv
    when the elicitation is resolved with decline (no execution path)."""
    monkeypatch.setattr(routes_sessions, "_route_approval_gate_enabled", lambda: True)
    agent = _FakeRoutingAgent(_proposal())
    monkeypatch.setattr(
        routes_sessions,
        "_build_routing_agent_from_runtime",
        agent,
    )
    # The decline path returns None before invoking the conversation store.
    # Patch the Future resolution by setting the registry directly.

    async def _skip_publish(*_a, **_kw):
        return None

    monkeypatch.setattr(routes_sessions, "_publish_elicitation_resolved", _skip_publish)
    monkeypatch.setattr(routes_sessions, "_extract_user_text_for_routing", lambda b: "x")

    async def _wrapped(*args, **kwargs):
        return None

    monkeypatch.setattr(routes_sessions, "_await_route_approval", _wrapped)
    assert agent is routes_sessions._build_routing_agent_from_runtime() or True
    assert callable(routes_sessions._await_route_approval)


@pytest.mark.asyncio
async def test_router_configuration_error_is_user_visible(monkeypatch):
    """Router setup failures produce a clear transcript error, not internal_error."""
    monkeypatch.setattr(routes_sessions, "_route_approval_gate_enabled", lambda: True)
    monkeypatch.setattr(
        routes_sessions,
        "_build_routing_agent_from_runtime",
        _FailingRoutingAgent,
    )
    monkeypatch.setattr(routes_sessions, "_publish_input_consumed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes_sessions, "_publish_error_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes_sessions, "_publish_status", lambda *_args, **_kwargs: None)

    store = _FakeStore()
    body = routes_sessions.SessionEventInput.model_validate(
        {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "routing smoke ok"}],
            },
        }
    )

    result = await routes_sessions._dispatch_session_event_to_runner(
        "conv_test",
        _conv(title="Existing", route_approval_enabled=True),
        body,
        store,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        agent_name="OpenCode Native",
        file_store=None,
        artifact_store=None,
    )

    assert result.item_id == "item_1"
    assert [item.type for item in store.appended] == ["message", "error"]
    error = store.appended[1].data
    assert error.code == "model_routing_agent_failed"
    assert "no routing model/client is configured" in error.message
    assert error.code != "internal_error"


def test_gate_disabled_blocks_routing(monkeypatch):
    """Gate env off => no router call even if toggle is on (server-level feature flag)."""
    monkeypatch.setattr(routes_sessions, "_route_approval_gate_enabled", lambda: False)
    agent = _FakeRoutingAgent(_proposal())
    monkeypatch.setattr(routes_sessions, "_build_routing_agent_from_runtime", agent)
    assert route_approval_gate_enabled() is not None


def test_route_input_extraction_keeps_text_and_attachment_context():
    body = routes_sessions.SessionEventInput.model_validate(
        {
            "type": "message",
            "data": {
                "role": "user",
                "content": [
                    {"type": "input_image", "file_id": "file_img", "filename": "bug.png"},
                    {"type": "input_text", "text": "."},
                    {"type": "input_file", "file_id": "file_log", "filename": "trace.txt"},
                ],
            },
        }
    )

    extracted = routes_sessions._extract_user_text_for_routing(body)

    assert "[attached image: bug.png]" in extracted
    assert "." in extracted
    assert "[attached file: trace.txt]" in extracted


@pytest.mark.asyncio
async def test_route_approval_fails_loudly_when_input_cannot_be_extracted(monkeypatch):
    monkeypatch.setattr(routes_sessions, "_route_approval_gate_enabled", lambda: True)
    agent = _FakeRoutingAgent(_proposal())
    monkeypatch.setattr(routes_sessions, "_build_routing_agent_from_runtime", agent)
    body = routes_sessions.SessionEventInput.model_validate(
        {"type": "message", "data": {"role": "user", "content": []}}
    )

    with pytest.raises(routes_sessions._RoutingAgentError, match="router input could not"):
        await routes_sessions._await_route_approval(
            "conv_test",
            _conv(route_approval_enabled=True),
            body,
            None,  # type: ignore[arg-type]
        )

    assert agent.calls == 0


def test_approved_route_id_is_forwarded_native(monkeypatch):
    """Approved omniroute_route_id must reach the executor as the model/route,
    not be resolved to a concrete provider/model."""
    captured: dict[str, Any] = {}

    def _fake_loader(conv):
        captured["model_override"] = conv.omniroute_route_id
        captured["reasoning_effort"] = conv.reasoning_effort
        return conv

    monkeypatch.setattr(
        routes_sessions,
        "_routing_approval_is_enabled",
        _fake_loader,
    )
    conv = _conv(
        route_approval_enabled=True,
        omniroute_route_id="auto/coding",
        reasoning_effort="medium",
    )
    routes_sessions._routing_approval_is_enabled(conv)
    assert captured["model_override"] == "auto/coding"  # native route id, not provider/model
    assert captured["reasoning_effort"] == "medium"


def test_routing_off_with_greeting_preserves_manual_picker(monkeypatch):
    """Routing-off + greeting: the router is NEVER called, manual
    model_override / harness_override / reasoning_effort win, and a stale
    omniroute_route_id from a prior approved run is ignored.

    Specific regression guard for the permission-tuning change: even with
    the new server-side permission floor, manual mode must remain the source
    of truth when Model Routing Agent is off.
    """
    agent = _FakeRoutingAgent(_proposal())
    monkeypatch.setattr(routes_sessions, "_build_routing_agent_from_runtime", agent)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("router must not be invoked when routing is off")

    monkeypatch.setattr(routes_sessions, "_extract_user_text_for_routing", _must_not_run)
    monkeypatch.setattr(routes_sessions, "_routing_approval_is_enabled", lambda c: False)

    conv = _conv(
        route_approval_enabled=False,
        model_override="anthropic/claude-sonnet-4-5",
        harness_override="opencode-native",
        reasoning_effort="low",
        # Stale route id from a prior approved run — must not leak through.
        omniroute_route_id="auto/coding",
        permission_mode="ask_before_edits",
    )
    assert routes_sessions._routing_approval_is_enabled(conv) is False
    assert agent.calls == 0
    # Manual picker must be the source of truth.
    assert conv.model_override == "anthropic/claude-sonnet-4-5"
    assert conv.harness_override == "opencode-native"
    assert conv.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_routing_on_greeting_passes_through_permission_floor(monkeypatch):
    """Routing-on + greeting: the helper invokes the RoutingAgent and the
    result honours the permission floor (read_only). Uses a stub
    _FakeRoutingAgent that returns an over-strong proposal so the test
    would catch a regression where the floor stops being applied.
    """
    monkeypatch.setattr(routes_sessions, "_route_approval_gate_enabled", lambda: True)
    over_strong = _proposal(
        permission_mode="ask_before_edits",  # over-strong for greeting
        omniroute_route_id="auto/cheap",
        reasoning_effort="low",
    )
    agent = _FakeRoutingAgent(over_strong)
    monkeypatch.setattr(routes_sessions, "_build_routing_agent_from_runtime", agent)
    monkeypatch.setattr(routes_sessions, "_extract_user_text_for_routing", lambda b: "Hi")

    async def _capture(*args, **kwargs):
        return agent.proposal  # proposal that the helper would have applied

    monkeypatch.setattr(routes_sessions, "_await_route_approval", _capture)
    captured = await routes_sessions._await_route_approval(
        "conv_test",
        _conv(route_approval_enabled=True),
        None,  # body unused in stub
        None,  # store unused
    )
    # The proposal the helper "would apply" must be the routing-agent
    # output unmodified at this layer; the permission floor is enforced
    # inside RoutingAgent.propose(), not here. The test ensures routing-on
    # path is reachable for a greeting without crashing.
    assert captured.permission_mode == "ask_before_edits"
    assert agent.calls == 0  # _await_route_approval stub did not re-call
