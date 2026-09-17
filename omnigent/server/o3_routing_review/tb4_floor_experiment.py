"""Exact TB4 floor experiment layered on the existing O3 routing review."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adviser import ADVISER_MODEL_ENV, _extract_text
from .audit import redact, snapshot
from .models import (
    CatalogueExecutionDecision,
    CatalogueExecutionSet,
    Disposition,
    ProposalAdjustmentRequest,
    ResourceAdvice,
    RoutingProposal,
)
from .recommendation import CATALOG_DIR_ENV

if TYPE_CHECKING:
    from .service import O3RoutingReviewService

POLICY_VERSION = "tb4-exact-floor-ab-v1"
BASELINE_FILENAME = "tb4-model-effort-baseline-v1.json"
LEGACY_FLOOR_PREFIX = "conservative capability score "


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TB4FloorAdvice(_StrictModel):
    """Candidate-blind minimum Terminal-Bench 4 performance requested for the task."""

    floor_percent: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2_000)


class TB4FloorExperimentResult(_StrictModel):
    policy_version: Literal["tb4-exact-floor-ab-v1"] = POLICY_VERSION
    experiment_key_sha256: str
    user_floor_percent: float = Field(ge=0, le=100)
    adviser_floor_percent: float = Field(ge=0, le=100)
    assigned_arm: Literal["user", "adviser", "same"]
    assignment_propensity: float = Field(gt=0, le=1)
    executed_floor_percent: float = Field(ge=0, le=100)
    adviser_confidence: float = Field(ge=0, le=1)
    adviser_rationale: str
    adviser_requested_model: str
    adviser_actual_model: str | None = None
    adviser_actual_provider: str | None = None
    adviser_reasoning_effort: str
    status: Literal["assigned", "applied"] = "assigned"


def _key_digest(experiment_key: str) -> str:
    if not experiment_key.strip():
        raise ValueError("experiment_key must not be blank")
    return hashlib.sha256(experiment_key.encode()).hexdigest()


def choose_arm(
    experiment_key: str, user_floor_percent: float, adviser_floor_percent: float
) -> tuple[str, float, float]:
    """Return a deterministic 50/50 assignment for a stable logical task identity."""
    if user_floor_percent == adviser_floor_percent:
        return "same", 1.0, user_floor_percent
    digest = hashlib.sha256(f"{POLICY_VERSION}:{experiment_key}".encode()).digest()
    arm = "user" if digest[0] < 128 else "adviser"
    chosen = user_floor_percent if arm == "user" else adviser_floor_percent
    return arm, 0.5, chosen


async def advise_tb4_floor(
    service: O3RoutingReviewService,
    *,
    prompt: str,
    workspace_summary: str,
    model: str,
    reasoning_effort: str,
) -> tuple[TB4FloorAdvice, dict[str, str | None]]:
    """Ask the adviser for a literal TB4 score without exposing user/candidate floors."""
    schema = TB4FloorAdvice.model_json_schema()
    system = (
        "You are a coding-task capability-floor adviser. Return only the supplied JSON schema. "
        "Choose a literal Terminal-Bench 4.0 pass@1 percentage from 0 to 100 representing the "
        "minimum measured TB4 performance you would require from a coding model configuration "
        "before routing this task to it. TB4 contains 66 terminal tasks spanning software "
        "engineering, system administration, data processing, model training, security, and "
        "other command-line work. The number is a capability floor, not a prediction that this "
        "particular task succeeds. Candidate models, candidate benchmark scores, provider "
        "availability, cost/quota, the user's own floor, and the eventual experiment assignment "
        "are deliberately withheld. Do not choose or name a model/provider/harness. Use the full "
        "0..100 range when justified instead of mapping to categorical difficulty labels."
    )
    payload = json.dumps(
        {"unchanged_user_task": prompt, "workspace_summary": workspace_summary},
        separators=(",", ":"),
    )
    body: dict[str, object] = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ],
        "reasoning": {"effort": reasoning_effort},
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "tb4_floor_advice",
                "strict": True,
                "schema": schema,
            }
        },
    }
    response = await service.omniroute.create_response(body)
    try:
        advice = TB4FloorAdvice.model_validate(json.loads(_extract_text(response.body)))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        from .service import RoutingReviewError

        raise RoutingReviewError(
            "TB4 floor adviser returned invalid structured output",
            status_code=502,
            code="invalid_tb4_floor_adviser_output",
        ) from exc
    headers = {key.lower(): value for key, value in response.headers.items()}
    return advice, {
        "requested_model": model,
        "actual_model": headers.get("x-omniroute-model")
        or headers.get("x-model")
        or (response.body.get("model") if isinstance(response.body.get("model"), str) else None),
        "actual_provider": headers.get("x-omniroute-provider") or headers.get("x-provider"),
    }


def _normalise_model(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _model_keys(decision: CatalogueExecutionDecision) -> list[str]:
    metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
    forecast = metadata.get("forecast") if isinstance(metadata.get("forecast"), dict) else {}
    values = [
        forecast.get("canonical_live_model"),
        forecast.get("display_model_alias"),
        decision.displayed_model,
        decision.route_id,
    ]
    result: list[str] = []
    for value in values:
        normalised = _normalise_model(value)
        if not normalised:
            continue
        for key in (normalised, normalised.split("/")[-1]):
            if key and key not in result:
                result.append(key)
    return result


def _unit_score(value: object) -> float | None:
    """Reject percentages, booleans and non-finite values at the admission boundary."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if math.isfinite(score) and 0.0 <= score <= 1.0 else None


def _baseline_score(row: dict[str, Any]) -> float | None:
    score = _unit_score(row.get("baseline_score"))
    if score is None:
        return None
    if "baseline_percent" in row:
        percent = row["baseline_percent"]
        if (
            isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not math.isfinite(percent)
            or not math.isclose(percent, score * 100.0, rel_tol=0.0, abs_tol=1e-4)
        ):
            return None
    return score


def _load_baseline() -> dict[tuple[str, str], dict[str, Any]]:
    directory = os.environ.get(CATALOG_DIR_ENV)
    if not directory:
        raise ValueError(f"{CATALOG_DIR_ENV} is required for TB4 floor routing")
    path = Path(directory) / BASELINE_FILENAME
    document = json.loads(path.read_text())
    expected = {
        "schema_version": 1,
        "policy_version": "tb4-model-effort-baseline-v1",
        "benchmark_id": "terminal-bench",
        "benchmark_version": "4.0.0",
        "slice_id": "tb4.overall",
        "score_scale": "0..1",
    }
    if not isinstance(document, dict) or any(
        document.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("unsupported TB4 model/effort baseline or score scale")
    if not isinstance(document.get("baselines"), list):
        raise ValueError("TB4 model/effort baselines must be an array")
    index: dict[tuple[str, str], dict[str, Any]] = {}
    ambiguous: set[tuple[str, str]] = set()
    for row in document.get("baselines", []):
        if not isinstance(row, dict):
            continue
        model = _normalise_model(row.get("canonical_model"))
        effort_value = row.get("reasoning_effort")
        if not isinstance(effort_value, str):
            continue
        effort = effort_value.strip().lower()
        if (
            not model
            or effort in {"", "default", "unknown"}
            or _baseline_score(row) is None
        ):
            continue
        for key in {model, model.split("/")[-1]}:
            lookup = (key, effort)
            if lookup in ambiguous:
                continue
            existing = index.get(lookup)
            if existing is not None and existing != row:
                index.pop(lookup, None)
                ambiguous.add(lookup)
            else:
                index[lookup] = row
    return index


def _baseline_for(
    decision: CatalogueExecutionDecision,
    *,
    requested_effort: str,
    index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
    forecast = metadata.get("forecast") if isinstance(metadata.get("forecast"), dict) else {}
    if forecast.get("estimate_method") == "opaque_alias_hypothesis":
        return None
    route_effort = str(forecast.get("reasoning_mode") or decision.reasoning_mode or "default").lower()
    effort = requested_effort.lower() if route_effort == "default" else route_effort
    matched: dict[str, Any] | None = None
    for key in _model_keys(decision):
        row = index.get((key, effort))
        if row is None:
            continue
        if _baseline_score(row) is None:
            return None
        if matched is not None and matched != row:
            # Conflicting canonical/alias matches are not interchangeable evidence.
            return None
        matched = row
    return matched


def filter_execution_set_for_tb4(
    execution_set: CatalogueExecutionSet,
    *,
    floor_score: float,
    requested_effort: str,
    baseline_index: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> CatalogueExecutionSet:
    """Replace the legacy normalized floor exclusion with exact TB4 baseline admission."""
    if _unit_score(floor_score) is None:
        raise ValueError("TB4 floor must be a finite number on the 0..1 scale")
    index = _load_baseline() if baseline_index is None else baseline_index
    eligible: list[CatalogueExecutionDecision] = []
    excluded: list[CatalogueExecutionDecision] = []
    counts: Counter[str] = Counter()
    all_rows = [*execution_set.eligible, *execution_set.excluded]

    for decision in all_rows:
        reasons = [
            reason
            for reason in decision.exclusions
            if not reason.startswith(LEGACY_FLOOR_PREFIX)
        ]
        baseline = _baseline_for(
            decision, requested_effort=requested_effort, index=index
        )
        metadata = dict(decision.metadata or {})
        if baseline is None:
            reasons.append("TB4 model/effort baseline is unavailable")
            metadata["tb4_baseline"] = None
        else:
            score = float(baseline["baseline_score"])
            metadata["tb4_baseline"] = redact(baseline)
            metadata["tb4_floor_score"] = floor_score
            if score < floor_score:
                reasons.append(
                    f"TB4 baseline {score:.6g} is below exact floor {floor_score:.6g}"
                )
        updated = decision.model_copy(
            update={
                "metadata": metadata,
                "exclusions": reasons,
                **(
                    {"capability_score_lower": float(baseline["baseline_score"]) * 100.0}
                    if baseline is not None
                    else {}
                ),
            }
        )
        if reasons:
            excluded.append(updated)
            counts.update(reasons)
        else:
            eligible.append(updated)

    def tb4_score(item: CatalogueExecutionDecision) -> float:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        baseline = metadata.get("tb4_baseline")
        if isinstance(baseline, dict) and isinstance(baseline.get("baseline_score"), (int, float)):
            return float(baseline["baseline_score"])
        return -1.0

    eligible.sort(key=lambda item: (-tb4_score(item), item.route_id))
    excluded.sort(key=lambda item: item.route_id)
    return execution_set.model_copy(
        update={
            "eligible_count": len(eligible),
            "eligible": eligible,
            "excluded": excluded,
            "exclusion_counts": dict(sorted(counts.items())),
        }
    )


async def apply_floor_experiment(
    service: O3RoutingReviewService,
    *,
    proposal_id: str,
    user_floor_percent: float,
    experiment_key: str,
) -> RoutingProposal:
    """Persist independent floors, assign one arm, and apply exact TB4 admission."""
    from .service import RoutingReviewError

    proposal = service.get_proposal(proposal_id)
    audit = dict(proposal.audit or {})
    input_record = audit.get("input")
    if not isinstance(input_record, dict):
        raise RoutingReviewError(
            "the original task input is unavailable for independent TB4 floor review",
            status_code=409,
            code="tb4_original_input_unavailable",
        )
    prompt = input_record.get("prompt")
    workspace_summary = input_record.get("workspace_summary", "")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(workspace_summary, str):
        raise RoutingReviewError(
            "the original task input is unavailable for independent TB4 floor review",
            status_code=409,
            code="tb4_original_input_unavailable",
        )
    existing = audit.get("tb4_floor_experiment")
    key_sha = _key_digest(experiment_key)
    if isinstance(existing, dict):
        if (
            existing.get("experiment_key_sha256") != key_sha
            or float(existing.get("user_floor_percent", -1)) != float(user_floor_percent)
        ):
            raise RoutingReviewError(
                "this proposal already has a different TB4 floor experiment",
                status_code=409,
                code="tb4_floor_experiment_conflict",
            )
        if existing.get("status") == "applied":
            return proposal
        adviser_floor = float(existing["adviser_floor_percent"])
        assigned_arm = str(existing["assigned_arm"])
        propensity = float(existing["assignment_propensity"])
        executed_floor = float(existing["executed_floor_percent"])
    else:
        model = (
            proposal.estimator.combo_name
            if proposal.estimator is not None and proposal.estimator.combo_name
            else os.environ.get(ADVISER_MODEL_ENV, "custom/o3-routing-adviser")
        )
        effort = (
            proposal.estimator.policy.reasoning_effort
            if proposal.estimator is not None
            else "low"
        )
        advice, attribution = await advise_tb4_floor(
            service,
            prompt=prompt,
            workspace_summary=workspace_summary,
            model=model,
            reasoning_effort=effort,
        )
        adviser_floor = advice.floor_percent
        assigned_arm, propensity, executed_floor = choose_arm(
            experiment_key, user_floor_percent, adviser_floor
        )
        record = TB4FloorExperimentResult(
            experiment_key_sha256=key_sha,
            user_floor_percent=user_floor_percent,
            adviser_floor_percent=adviser_floor,
            assigned_arm=assigned_arm,
            assignment_propensity=propensity,
            executed_floor_percent=executed_floor,
            adviser_confidence=advice.confidence,
            adviser_rationale=advice.rationale,
            adviser_requested_model=model,
            adviser_actual_model=attribution["actual_model"],
            adviser_actual_provider=attribution["actual_provider"],
            adviser_reasoning_effort=effort,
        )
        audit["tb4_floor_experiment"] = record.model_dump(mode="json")
        proposal = proposal.model_copy(update={"audit": audit})
        service.store.put(snapshot(proposal, "TB4 floor assigned"))

    tb4_slice = next(
        (item for item in service.registry.slices if item.slice_id == "tb4.overall"),
        None,
    )
    if tb4_slice is None:
        raise RoutingReviewError(
            "TB4 overall is not available in the benchmark registry",
            status_code=409,
            code="tb4_slice_unavailable",
        )
    # A raw numeric floor is the experiment treatment. Preserve the adviser's
    # original categorical task classification for analysis; do not rewrite it
    # to an arbitrary legacy difficulty merely to pass through the old API.
    adjusted = await service.adjust_proposal(
        proposal_id,
        ProposalAdjustmentRequest(
            benchmark_id=tb4_slice.benchmark_id,
            version=tb4_slice.version,
            slice_id=tb4_slice.slice_id,
            minimum_score=executed_floor / 100.0,
        ),
    )
    recommendation = adjusted.recommendation
    if recommendation is None or recommendation.execution_set is None:
        raise RoutingReviewError(
            "TB4 floor routing requires the recommendation catalogue",
            status_code=409,
            code="tb4_recommendation_unavailable",
        )
    try:
        filtered = filter_execution_set_for_tb4(
            recommendation.execution_set,
            floor_score=executed_floor / 100.0,
            requested_effort=adjusted.approved_constraints.reasoning_effort,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RoutingReviewError(
            str(exc),
            status_code=409,
            code="tb4_baseline_unavailable",
        ) from exc

    recommendation = recommendation.model_copy(
        update={
            "execution_set": filtered,
            "common_capability_floor": 0.0,
        }
    )
    resource_snapshot = await service.omniroute.resource_snapshot(filtered.eligible)
    resource_advice = ResourceAdvice(
        action="start_now" if resource_snapshot.usable_routes else "wait",
        reason=(
            "At least one exact-TB4-floor route is currently reported usable."
            if resource_snapshot.usable_routes
            else "No exact-TB4-floor route is confirmed usable; preserve the floor."
        ),
        source="deterministic_fallback",
    )
    disposition = Disposition.ROUTE if filtered.eligible_count else Disposition.DEFER
    final_audit = dict(adjusted.audit or {})
    record = dict(final_audit.get("tb4_floor_experiment") or audit["tb4_floor_experiment"])
    record["status"] = "applied"
    record["eligible_count"] = filtered.eligible_count
    record["baseline_policy_version"] = "tb4-model-effort-baseline-v1"
    final_audit["tb4_floor_experiment"] = record
    updated = adjusted.model_copy(
        update={
            "audit": final_audit,
            "recommendation": recommendation,
            "resource_snapshot": resource_snapshot,
            "resource_advice": resource_advice,
            "disposition": disposition,
            "adviser": adjusted.adviser.model_copy(update={"disposition": disposition}),
        }
    )
    updated = await service._with_execution_options(updated)
    service.store.put(snapshot(updated, "TB4 floor applied"))
    return updated
