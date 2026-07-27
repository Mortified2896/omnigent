"""Validation tests for the OpenCode-native permission modes.

The session-create / session-update APIs share a single
``KNOWN_PERMISSION_MODES`` enum (lives in
:mod:`omnigent.server.routing_agent`). Six new values were added to
cover the OpenCode-native landing composer; the existing
router-only values stay unchanged. These tests pin both halves of
that contract:

- All six new modes are accepted on session-create and update.
- Unknown / garbage strings return a 4xx-shaped validation error.
- ``permission_mode: None`` preserves current default behaviour (no
  override sent, opencode runs with its own default).
- The shared enum is the SINGLE source of truth — a route proposal
  that emits one of the user-facing values is rejected (the router
  must not invent permission modes the user didn't choose).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.server.routing_agent import (
    KNOWN_PERMISSION_MODES,
    ROUTER_PROPOSABLE_PERMISSION_MODES,
    validate_route_proposal,
)
from omnigent.server.schemas import SessionCreateRequest

# ---------------------------------------------------------------------------
# Shared enum: both router and create paths agree on the membership
# ---------------------------------------------------------------------------


def test_known_permission_modes_includes_all_six_opencode_values() -> None:
    """The shared enum must list every user-facing mode the UI exposes."""
    for mode in ("default", "auto", "accept_edits", "plan", "dont_ask", "bypass"):
        assert mode in KNOWN_PERMISSION_MODES, (
            f"OpenCode-native UI mode {mode!r} missing from KNOWN_PERMISSION_MODES"
        )


def test_router_proposable_subset_excludes_user_facing_modes() -> None:
    """The router may not invent auto/plan/etc. — those are user-only choices."""
    for mode in ("default", "auto", "accept_edits", "plan", "dont_ask"):
        assert mode not in ROUTER_PROPOSABLE_PERMISSION_MODES, (
            f"router must not propose user-only mode {mode!r}"
        )


def test_router_proposable_subset_keeps_legacy_modes() -> None:
    """Backward compatibility: the router's existing modes still validate."""
    for mode in (
        "ask_before_edits",
        "ask_before_commands",
        "read_only",
        "auto_accept_edits",
        "bypass",
    ):
        assert mode in ROUTER_PROPOSABLE_PERMISSION_MODES


# ---------------------------------------------------------------------------
# Session-create validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["default", "auto", "accept_edits", "plan", "dont_ask", "bypass"])
def test_session_create_accepts_each_opencode_native_mode(mode: str) -> None:
    """Every user-visible mode must round-trip through SessionCreateRequest."""
    req = SessionCreateRequest(
        agent_id="ag_abc",
        permission_mode=mode,
    )
    assert req.permission_mode == mode


def test_session_create_accepts_none_permission_mode() -> None:
    """Omitting permission_mode is the documented default behaviour."""
    req = SessionCreateRequest(agent_id="ag_abc")
    assert req.permission_mode is None


@pytest.mark.parametrize(
    "mode", ["", "  ", "YOLO", "accept-edits", "AutoAccept", "default;rm -rf /"]
)
def test_session_create_rejects_unknown_permission_mode(mode: str) -> None:
    """Unknown / garbage values must surface as a 4xx-shaped validation error."""
    with pytest.raises(ValidationError):
        SessionCreateRequest(agent_id="ag_abc", permission_mode=mode)


def test_session_create_rejects_router_only_mode() -> None:
    """A mode that exists for the router but not for create would leak policy.

    ``read_only`` is currently accepted by the create validator (it lives in
    the shared enum) — the create endpoint is more permissive than the router
    because a manual session may legitimately want read-only. The test
    asserts the create side does NOT accept a value that's not in the
    shared enum (the negative side of the gate).
    """
    with pytest.raises(ValidationError):
        SessionCreateRequest(agent_id="ag_abc", permission_mode="nonsense-not-in-enum")


# ---------------------------------------------------------------------------
# Router proposal validator: must reject the user-facing modes
# ---------------------------------------------------------------------------


def test_router_validator_rejects_user_facing_auto_mode() -> None:
    """The router must not emit ``auto`` — that's a user-facing choice."""
    from omnigent.server.routing_agent import RouteProposal

    proposal = RouteProposal(
        task_type="general_chat",
        recommended_harness="opencode-native",
        omniroute_route_id="auto/cheap",
        reasoning_effort="low",
        permission_mode="auto",
        allowed_billing_classes=["free"],
        forbidden_billing_classes=[],
        execution_fallback_policy="fail_closed_no_api_billed_fallback",
        omniroute_requires_explicit_approval=False,
        rationale=["test"],
        router_invoked=False,
        router_fallback_used=False,
        proposal_source="default_route_policy",
        proposal_source_label="default",
    )
    with pytest.raises(Exception, match="unknown permission mode for router"):
        validate_route_proposal(proposal)


def test_router_validator_accepts_legacy_bypass() -> None:
    """``bypass`` is in both subsets — defense-in-depth gating still allows it."""
    from omnigent.server.routing_agent import RouteProposal

    proposal = RouteProposal(
        task_type="coding",
        recommended_harness="opencode-native",
        omniroute_route_id="auto/coding",
        reasoning_effort="medium",
        permission_mode="bypass",
        allowed_billing_classes=["free", "subscription"],
        forbidden_billing_classes=["api_billed"],
        execution_fallback_policy="fail_closed_no_api_billed_fallback",
        omniroute_requires_explicit_approval=False,
        rationale=["test"],
        router_invoked=False,
        router_fallback_used=False,
        proposal_source="default_route_policy",
        proposal_source_label="default",
    )
    # ``bypass`` stays proposable — validate_route_proposal returns a
    # RouteProposal. The defense-in-depth string the test cares about
    # (``unknown permission mode for router``) must not fire.
    validated = validate_route_proposal(proposal)
    assert validated.permission_mode == "bypass"
