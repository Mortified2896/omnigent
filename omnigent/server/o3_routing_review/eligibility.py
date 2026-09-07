"""Full-catalogue capability and effective-I/O eligibility."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from .models import (
    CatalogueExecutionDecision,
    CatalogueExecutionSet,
    ReasoningEffort,
    RoutingRequirements,
)

if TYPE_CHECKING:
    from .recommendation import RecommendationCatalogue

_CALLABLE = {"callable", "callable_now", "success", "responses_callable"}
_SUPPORTED_RESPONSES = {"catalog_declared", "proven"}


def _string_set(value: object) -> set[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return {item.lower() for item in value}


def _require_modalities(
    exclusions: list[str], *, required: list[str], supported: object, direction: str
) -> None:
    wanted = {item.lower() for item in required}
    available = _string_set(supported)
    if not wanted:
        return
    if available is None:
        exclusions.append(f"{direction} modalities are unknown")
        return
    missing = sorted(wanted - available)
    if missing:
        exclusions.append(f"missing required {direction} modalities: {', '.join(missing)}")


def _require_limit(exclusions: list[str], *, required: int, supported: object, label: str) -> None:
    if required <= 0:
        return
    if not isinstance(supported, int):
        exclusions.append(f"{label} limit is unknown")
    elif supported < required:
        exclusions.append(f"{label} limit {supported} is below required {required}")


def build_execution_set(
    catalogue: RecommendationCatalogue,
    *,
    common_floor: float,
    requirements: RoutingRequirements,
    reasoning_effort: ReasoningEffort,
    live_route_ids: set[str],
) -> CatalogueExecutionSet:
    """Evaluate every catalogue route/configuration without UI truncation."""
    eligible: list[CatalogueExecutionDecision] = []
    excluded: list[CatalogueExecutionDecision] = []
    counts: Counter[str] = Counter()

    rows_by_route = {str(row["provider_model_route_id"]): row for row in catalogue.forecasts}
    for route_id in sorted(rows_by_route.keys() | live_route_ids):
        row = rows_by_route.get(route_id, {})
        provider_id = str(row.get("provider") or route_id.split("/", 1)[0])
        reasoning_mode = str(row.get("reasoning_mode") or "default")
        ready: dict[str, Any] = catalogue.readiness.get(route_id, {})
        exclusions: list[str] = []
        basis: list[str] = []
        lower = row.get("capability_score_lower")

        if route_id not in live_route_ids:
            exclusions.append("route is not in the current exposed catalogue")
        if not row.get("adviser_applicable", False) or not isinstance(lower, (int, float)):
            exclusions.append("conservative capability evidence is unavailable")
        elif float(lower) < common_floor:
            exclusions.append(
                f"conservative capability score {float(lower):g} is below floor {common_floor:g}"
            )

        if reasoning_mode not in {reasoning_effort, "default"}:
            exclusions.append(
                f"reasoning configuration {reasoning_mode!r} does not satisfy "
                f"approved {reasoning_effort!r}"
            )

        required_inputs = list(requirements.input_modalities)
        if requirements.vision and "image" not in required_inputs:
            required_inputs.append("image")
        _require_modalities(
            exclusions,
            required=required_inputs,
            supported=row.get("input_modalities"),
            direction="input",
        )
        _require_modalities(
            exclusions,
            required=requirements.output_modalities,
            supported=row.get("output_modalities"),
            direction="output",
        )
        _require_limit(
            exclusions,
            required=max(requirements.minimum_context_tokens, requirements.minimum_input_tokens),
            supported=row.get("context_window"),
            label="input/context",
        )
        _require_limit(
            exclusions,
            required=requirements.minimum_output_tokens,
            supported=row.get("max_output_tokens"),
            label="output",
        )

        if requirements.tools:
            if row.get("tool_calling") is not True:
                exclusions.append("tool calling is not supported")
            else:
                basis.append("catalogue tool-calling support")
        if requirements.structured_output:
            structured = row.get("structured_output", row.get("structured_outputs"))
            if structured is not True:
                exclusions.append("structured output support is unresolved")
            else:
                basis.append("catalogue structured-output support")

        callability = str(ready.get("responses_callability") or "unknown")
        responses = str(row.get("responses_compatibility") or "unknown")
        runtime_compatibility = str(row.get("runtime_compatibility") or "unknown")
        if responses in _SUPPORTED_RESPONSES:
            basis.append(f"Responses support: {responses}")
        elif runtime_compatibility == "proven":
            basis.append("OmniRoute adapter compatibility: proven")
        elif callability in _CALLABLE:
            basis.append(f"OmniRoute adapter observation: {callability}")
        else:
            exclusions.append("Responses client-contract compatibility is unresolved")

        identity = ready.get("equivalence_key")
        decision = CatalogueExecutionDecision(
            route_id=route_id,
            provider_id=provider_id,
            displayed_model=str(row.get("display_model_alias") or route_id.split("/", 1)[-1]),
            reasoning_mode=reasoning_mode,
            capability_score_lower=float(lower) if isinstance(lower, (int, float)) else None,
            equivalence_identity=str(identity) if isinstance(identity, str) and identity else None,
            compatibility_basis=basis,
            exclusions=exclusions,
        )
        if exclusions:
            excluded.append(decision)
            counts.update(exclusions)
        else:
            eligible.append(decision)

    eligible.sort(key=lambda item: (-float(item.capability_score_lower or 0), item.route_id))
    excluded.sort(key=lambda item: item.route_id)
    return CatalogueExecutionSet(
        total_evaluated=len(rows_by_route.keys() | live_route_ids),
        eligible_count=len(eligible),
        eligible=eligible,
        excluded=excluded,
        exclusion_counts=dict(sorted(counts.items())),
    )
