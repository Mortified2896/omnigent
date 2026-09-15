from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from omnigent.server.o3_routing_review.models import DecisionAction
from omnigent.server.o3_routing_review.service import RoutingReviewError
from omnigent.server.o3_routing_review.session_policy import (
    protect_recorded_policy,
    validate_approved_launch,
)


def launch():
    proposal = Mock(
        proposal_id="review1",
        decision=DecisionAction.APPROVE,
        session_id=None,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        selected_execution=None,
        derived_combo_name="custom/o3-route-abcdef01",
    )
    proposal.approved_constraints.reasoning_effort = "low"
    request = {
        "labels": {"omnigent.routing_policy": "benchmark", "o3.routing.proposal_id": "review1"},
        "model_override": proposal.derived_combo_name,
        "reasoning_effort": "low",
        "harness_override": "codex-native",
        "cost_control_mode_override": "off",
    }
    return proposal, request


def test_approved_configuration_can_launch():
    proposal, request = launch()
    validate_approved_launch(request, proposal)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_override", "default"),
        ("reasoning_effort", "high"),
        ("harness_override", "auto"),
        ("cost_control_mode_override", "on"),
        ("subagent_routing_override", "on"),
    ],
)
def test_unreviewed_launch_configuration_is_rejected(field, value):
    proposal, request = launch()
    request[field] = value
    with pytest.raises(RoutingReviewError):
        validate_approved_launch(request, proposal)


def test_replayed_approval_cannot_launch_again():
    proposal, request = launch()
    proposal.session_id = "existing"
    with pytest.raises(RoutingReviewError):
        validate_approved_launch(request, proposal)


@pytest.mark.parametrize(
    "patch",
    [
        {"model_override": "default"},
        {"reasoning_effort": "high"},
        {"cost_control_mode_override": "on"},
        {"subagent_routing_override": "on"},
        {"labels": {"omnigent.routing_policy": "manual"}},
        {"labels": {"o3.routing.proposal_id": None}},
    ],
)
def test_saved_benchmark_configuration_cannot_escape_approval(patch):
    _proposal, request = launch()
    with pytest.raises(RoutingReviewError):
        protect_recorded_policy(request["labels"], request, patch)


def test_reconnect_and_unrelated_settings_keep_approval():
    _proposal, request = launch()
    protect_recorded_policy(
        request["labels"],
        request,
        {
            "title": "Renamed task",
            "runner_id": "reconnected",
            "reasoning_effort": "low",
        },
    )


def test_manual_model_changes_retain_native_behavior():
    protect_recorded_policy(
        {"omnigent.routing_policy": "manual"}, {}, {"model_override": "new-model"}
    )
