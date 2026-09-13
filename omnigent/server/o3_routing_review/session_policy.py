"""Keep approved routing fixed at session creation and metadata boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from .models import DecisionAction, ExecutionMode, RoutingProposal
from .service import RoutingReviewError

POLICY_LABEL = "omnigent.routing_policy"
PROPOSAL_LABEL = "o3.routing.proposal_id"
_ROUTE_FIELDS = frozenset(
    {
        "model_override",
        "reasoning_effort",
        "harness_override",
        "cost_control_mode_override",
        "subagent_routing_override",
        "terminal_launch_args",
    }
)


def validate_approved_launch(request: Mapping[str, object], proposal: RoutingProposal) -> None:
    """Reject a launch that differs from its approved execution configuration."""
    labels = request.get("labels")
    if not isinstance(labels, dict):
        raise RoutingReviewError("approved launch requires routing labels", status_code=409)
    if (
        labels.get(POLICY_LABEL) != "benchmark"
        or labels.get(PROPOSAL_LABEL) != proposal.proposal_id
    ):
        raise RoutingReviewError(
            "routing labels differ from the approved proposal", status_code=409
        )
    if proposal.decision not in {DecisionAction.APPROVE, DecisionAction.RUN_ANYWAY}:
        raise RoutingReviewError("review and approve this task before execution", status_code=409)
    if proposal.session_id is not None or proposal.expires_at <= datetime.now(timezone.utc):
        raise RoutingReviewError(
            "proposal is already used or expired; obtain a fresh review", status_code=409
        )
    tool_free = (
        proposal.selected_execution is not None
        and proposal.selected_execution.mode is ExecutionMode.HARD_TOOL_FREE
    )
    model = "local-tool-free/" + proposal.proposal_id if tool_free else proposal.derived_combo_name
    expected = {
        "model_override": model,
        "reasoning_effort": proposal.approved_constraints.reasoning_effort,
        "harness_override": "local-tool-free" if tool_free else "codex-native",
        "cost_control_mode_override": "off",
    }
    if not model or any(request.get(key) != value for key, value in expected.items()):
        raise RoutingReviewError(
            "launch differs from the approved model, harness or reasoning; review again",
            status_code=409,
        )
    if request.get("subagent_routing_override") not in (None, "off"):
        raise RoutingReviewError(
            "native subagent routing is unavailable for benchmark tasks", status_code=409
        )


def protect_recorded_policy(
    labels: Mapping[str, str | None], current: Mapping[str, object], patch: Mapping[str, object]
) -> None:
    """Allow unrelated edits and reconnection without rewriting routing history."""
    new_labels = patch.get("labels")
    if isinstance(new_labels, dict):
        for key in (POLICY_LABEL, PROPOSAL_LABEL, "omnigent.routing_backend"):
            if key in new_labels and new_labels[key] != labels.get(key):
                raise RoutingReviewError(
                    "recorded routing identity cannot be relabelled; start a new reviewed task",
                    status_code=409,
                )
    if labels.get(POLICY_LABEL) != "benchmark" and not labels.get(PROPOSAL_LABEL):
        return
    for key in _ROUTE_FIELDS:
        if key in patch and patch[key] != current.get(key):
            raise RoutingReviewError(
                "benchmark model and reasoning are approval-bound; start a new reviewed task",
                status_code=409,
            )
