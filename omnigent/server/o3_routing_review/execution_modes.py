"""Execution contracts are independent of a task's optional tool requirement."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import Field

from .models import (
    CostQuotaPreference,
    ExecutionMode,
    ExecutionOption,
    RoutingRequirements,
    StrictModel,
)
from .tool_free import PROTOCOL_VERSION

if TYPE_CHECKING:
    from .models import RoutingProposal
    from .recommendation import RecommendationCatalogue


class ToolFreeQualification(StrictModel):
    route: str
    provider: str
    protocol: str
    observed_at: datetime
    available: bool
    cost_class: str
    context_tokens: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    structured_output: bool | None = None
    exclusion: str | None = None


def tool_free_exclusions(
    qualification: ToolFreeQualification,
    requirements: RoutingRequirements,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Intersect provider evidence with the actual executor's capabilities."""
    reasons: list[str] = []
    if requirements.tools:
        reasons.append("task requires tools")
    if qualification.protocol != PROTOCOL_VERSION:
        reasons.append("hard tool-free protocol is not qualified")
    timestamp = qualification.observed_at
    if (
        timestamp.tzinfo is None
        or not 0 <= ((now or datetime.now(timezone.utc)) - timestamp).total_seconds() <= 900
    ):
        reasons.append("tool-free qualification is stale")
    if not qualification.available:
        reasons.append(qualification.exclusion or "tool-free route is unavailable")
    required_input = set(requirements.input_modalities)
    if requirements.vision:
        required_input.add("image")
    # The initial executor accepts text only; model abilities cannot extend it.
    if not required_input <= {"text"}:
        reasons.append("tool-free executor does not support required input modalities")
    elif not required_input <= set(qualification.input_modalities):
        reasons.append("provider does not support required input modalities")
    if not set(requirements.output_modalities) <= {"text"}:
        reasons.append("tool-free executor does not support required output modalities")
    elif not set(requirements.output_modalities) <= set(qualification.output_modalities):
        reasons.append("provider does not support required output modalities")
    if requirements.structured_output:
        reasons.append("tool-free structured-output contract is not implemented")
    minimum = max(requirements.minimum_context_tokens, requirements.minimum_input_tokens)
    if minimum and (
        qualification.context_tokens is None or qualification.context_tokens < minimum
    ):
        reasons.append("tool-free context window is unknown or below required minimum")
    if requirements.minimum_output_tokens and (
        qualification.max_output_tokens is None
        or min(qualification.max_output_tokens, 1024) < requirements.minimum_output_tokens
    ):
        reasons.append("tool-free output limit is below required minimum")
    return reasons


def select_execution(
    options: list[ExecutionOption],
    *,
    requirements: RoutingRequirements,
    floor: float,
    preference: CostQuotaPreference,
) -> ExecutionOption | None:
    """Price ranks only options that already meet the unmodified quality floor."""
    viable = [
        option
        for option in options
        if option.capability_score_lower >= floor
        and (not requirements.tools or option.mode is ExecutionMode.TOOL_CAPABLE_NATIVE)
    ]
    if not viable:
        return None
    preserve = preference in {"preserve_subscription", "lowest_cost"}
    return min(
        viable,
        key=lambda option: (
            -int(preserve and option.cost_class == "free"),
            -option.capability_score_lower,
            option.route,
            option.mode,
        ),
    )


def options_from_audit(
    proposal: RoutingProposal,
    catalogue: RecommendationCatalogue,
    audit: dict[str, Any],
    live_route_ids: set[str],
) -> list[ExecutionOption]:
    """Join fresh protocol observations to unchanged conservative quality evidence."""
    from .tool_search import qualified_route, read_capabilities

    options: list[ExecutionOption] = []
    recommendation = proposal.recommendation
    if recommendation is None or recommendation.execution_set is None:
        return options
    registry = read_capabilities()
    for item in recommendation.execution_set.eligible:
        if registry is not None and qualified_route(registry, item.provider_id, item.route_id):
            options.append(
                ExecutionOption(
                    mode=ExecutionMode.TOOL_CAPABLE_NATIVE,
                    route=item.route_id,
                    provider=item.provider_id,
                    cost_class="subscription" if item.provider_id == "codex" else "unknown",
                    capability_score_lower=item.capability_score_lower or 0,
                    reason="Meets the approved floor and qualified native tool contract",
                )
            )
    if proposal.approved_constraints.evidence_policy.value == "strict":
        return options
    forecasts = {row["provider_model_route_id"]: row for row in catalogue.forecasts}
    rows = audit.get("rows")
    if not isinstance(rows, list):
        return options
    for row in rows:
        if not isinstance(row, dict) or row.get("probe") != "passed":
            continue
        if row.get("reasoning_effort") != proposal.approved_constraints.reasoning_effort:
            continue
        route = row.get("route")
        if (
            not isinstance(route, str)
            or route not in live_route_ids
            or row.get("cost_class") not in {"free_label", "zero_priced"}
        ):
            continue
        # A cached answer or an unattributed response cannot prove live availability.
        if row.get("actual_provider") != row.get("provider") or row.get("cache") != "MISS":
            continue
        if row.get("actual_model") not in {route, route.split("/", 1)[-1]}:
            continue
        try:
            if float(row.get("reported_cost_usd", "unknown")) != 0:
                continue
        except (TypeError, ValueError):
            continue
        forecast = forecasts.get(route, {})
        lower = forecast.get("capability_score_lower")
        if not forecast.get("adviser_applicable") or not isinstance(lower, (int, float)):
            continue
        if lower < recommendation.common_capability_floor:
            continue
        if forecast.get("reasoning_mode", "default") not in {
            "default",
            proposal.approved_constraints.reasoning_effort,
        }:
            continue
        qualification = ToolFreeQualification(
            route=route,
            provider=row["provider"],
            protocol=row.get("protocol", ""),
            observed_at=audit["observed_at"],
            available=True,
            cost_class="free",
            context_tokens=row.get("context_tokens"),
        )
        if tool_free_exclusions(qualification, proposal.effective_requirements):
            continue
        options.append(
            ExecutionOption(
                mode=ExecutionMode.HARD_TOOL_FREE,
                route=route,
                provider=row["provider"],
                cost_class="free",
                capability_score_lower=lower,
                reason="Tools not required; meets floor; free route preserves subscription",
            )
        )
    return options
