"""Validated, immutable full-catalogue recommendations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .eligibility import build_execution_set
from .models import (
    CatalogueModelGroup,
    CatalogueRecommendation,
    CatalogueRecommendationItem,
    Difficulty,
    ReasoningEffort,
    RoutingRequirements,
)

CATALOG_DIR_ENV = "OMNIGENT_O3_RECOMMENDATION_CATALOG_DIR"
READINESS_FILENAME = "o3-route-readiness-v1.json"
READINESS_MANIFEST_FILENAME = "o3-route-readiness-manifest-v1.json"
REQUIRED_FILES = (
    "model-capability-estimation-policy-v1.json",
    "model-capability-forecasts-v1.json",
    READINESS_FILENAME,
    READINESS_MANIFEST_FILENAME,
)
_CONFIDENCE = {"high": 4, "medium": 3, "low": 2, "very_low": 1, "unknown": 0}
_CALLABLE = {"callable", "callable_now", "success", "responses_callable"}


@dataclass(frozen=True)
class RecommendationCatalogue:
    policy: dict[str, Any]
    forecasts: tuple[dict[str, Any], ...]
    readiness: dict[str, dict[str, Any]]
    manifest: dict[str, Any]
    hashes: dict[str, str]

    def floor(self, difficulty: Difficulty) -> float:
        try:
            return float(self.policy["normalization"]["anchors"][difficulty])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"catalogue policy has no valid {difficulty!r} common floor") from exc


def model_identity(row: dict[str, Any], ready: dict[str, Any]) -> str:
    """Group declared checkpoints; opaque hypotheses stay route-specific."""
    route_id = str(row["provider_model_route_id"])
    if row.get("estimate_method") == "opaque_alias_hypothesis":
        return str(ready.get("equivalence_key") or route_id)
    return str(
        row.get("inferred_base_checkpoint")
        or ready.get("equivalence_key")
        or row.get("canonical_live_model")
        or route_id
    )


def readiness_is_recent(observed: object, *, now: datetime | None = None) -> bool:
    if not isinstance(observed, str):
        return False
    try:
        timestamp = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
        return 0 <= age <= 900
    except ValueError:
        return False


def _read(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed O3 recommendation catalogue file: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"O3 recommendation catalogue file must be an object: {path.name}")
    return value, hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=4)
def load_recommendation_catalogue(directory: str) -> RecommendationCatalogue:
    root = Path(directory)
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(
            f"{CATALOG_DIR_ENV} must name a directory containing: {', '.join(REQUIRED_FILES)}; "
            f"missing: {', '.join(missing)}"
        )
    loaded = {name: _read(root / name) for name in REQUIRED_FILES}
    policy, forecasts, readiness, manifest = (loaded[name][0] for name in REQUIRED_FILES)
    if any(value.get("schema_version") != 1 for value in (policy, forecasts, readiness, manifest)):
        raise ValueError("unsupported O3 recommendation catalogue schema_version (expected 1)")
    forecast_rows, readiness_rows = forecasts.get("rows"), readiness.get("rows")
    if not isinstance(forecast_rows, list) or not isinstance(readiness_rows, list):
        raise ValueError("O3 recommendation catalogue rows must be arrays")
    if forecasts.get("route_count") != len(forecast_rows):
        raise ValueError("forecast route_count does not match rows")
    if readiness.get("route_count") != len(readiness_rows) or manifest.get("route_count") != len(
        readiness_rows
    ):
        raise ValueError("readiness route_count does not match rows or manifest")
    route_ids = [row.get("provider_model_route_id") for row in forecast_rows]
    ready_ids = [row.get("route_id") for row in readiness_rows]
    if any(not isinstance(value, str) for value in route_ids) or len(set(route_ids)) != len(
        route_ids
    ):
        raise ValueError("forecast route IDs must be strings and unique")
    if any(not isinstance(value, str) for value in ready_ids) or len(set(ready_ids)) != len(
        ready_ids
    ):
        raise ValueError("readiness route IDs must be strings and unique")
    represented = {
        **manifest.get("input_artifact_hashes", {}),
        **manifest.get("output_hashes", {}),
    }
    for name, (_, digest) in loaded.items():
        if name in represented and represented[name] != digest:
            raise ValueError(f"manifest hash mismatch for {name}")
    return RecommendationCatalogue(
        policy,
        tuple(forecast_rows),
        {row["route_id"]: row for row in readiness_rows},
        manifest,
        {name: value[1] for name, value in loaded.items()},
    )


def recommend(
    catalogue: RecommendationCatalogue,
    *,
    difficulty: Difficulty,
    raw_floor: float,
    live_route_ids: set[str],
    requirements: RoutingRequirements | None = None,
    reasoning_effort: ReasoningEffort = "low",
) -> CatalogueRecommendation:
    floor = catalogue.floor(difficulty)
    grouped: dict[tuple[str, str], list[CatalogueRecommendationItem]] = {}
    observed = catalogue.manifest.get("live_catalogue_timestamp")
    all_items: list[CatalogueRecommendationItem] = []
    for row in catalogue.forecasts:
        route_id = row["provider_model_route_id"]
        lower, central = row.get("capability_score_lower"), row.get("capability_score_central")
        if lower is None or central is None or not row.get("adviser_applicable", False):
            continue
        ready = catalogue.readiness.get(route_id, {})
        reasoning = str(row.get("reasoning_mode") or "default")
        identity = model_identity(row, ready)
        callability = str(ready.get("responses_callability") or "not_tested")
        caveats = [] if callability in _CALLABLE else [f"Responses readiness: {callability}"]
        if row.get("codex_tool_continuation_compatibility", "unknown") == "unknown":
            caveats.append("Execution compatibility is unknown; recommendation is advisory.")
        item = CatalogueRecommendationItem(
            route_id=route_id,
            provider_id=str(row.get("provider") or route_id.split("/", 1)[0]),
            displayed_model=str(row.get("display_model_alias") or route_id.split("/", 1)[-1]),
            equivalence_identity=identity,
            reasoning_mode=reasoning,
            capability_score_central=float(central),
            capability_score_lower=float(lower),
            capability_score_upper=float(row["capability_score_upper"])
            if row.get("capability_score_upper") is not None
            else None,
            estimated_tier=str(row.get("estimated_tier") or "unknown"),
            conservative_tier=str(row.get("conservative_tier") or "unknown"),
            capability_confidence=str(row.get("confidence") or "unknown"),
            estimate_method=str(row.get("estimate_method") or "unknown"),
            gap_from_floor=float(lower) - floor,
            live_present=route_id in live_route_ids,
            responses_callability=callability,
            readiness_observed_at=str(observed) if observed else None,
            operator_resource_class=str(ready.get("operator_resource_policy") or "unknown"),
            raw_cost_class=str(
                row.get("cost_class") or ready.get("catalog_cost_class") or "unknown"
            ),
            caveats=caveats,
        )
        all_items.append(item)
        grouped.setdefault((identity, reasoning), []).append(item)

    def rank(item: CatalogueRecommendationItem) -> tuple[float, float, int, int, str]:
        return (
            -item.capability_score_lower,
            -item.capability_score_central,
            -_CONFIDENCE.get(item.capability_confidence, 0),
            -int(item.responses_callability in _CALLABLE),
            item.route_id,
        )

    representatives, collapsed = [], 0
    for variants in grouped.values():
        variants.sort(
            key=lambda item: (
                -int(item.live_present),
                -int(item.capability_score_lower >= floor),
                -int(
                    "non_codex" in item.operator_resource_class
                    and item.responses_callability in _CALLABLE
                ),
                -int(item.live_present),
                *rank(item),
            )
        )
        representatives.append(
            variants[0].model_copy(
                update={"alternate_route_ids": [item.route_id for item in variants[1:]]}
            )
        )
        collapsed += len(variants) - 1
    present = [item for item in representatives if item.live_present]
    above = [item for item in present if item.capability_score_lower >= floor]
    callable_non_codex = [
        item
        for item in above
        if "non_codex" in item.operator_resource_class and item.responses_callability in _CALLABLE
    ]
    codex = [
        item
        for item in above
        if "codex" in item.operator_resource_class
        and "non_codex" not in item.operator_resource_class
    ]
    used = {item.route_id for item in callable_non_codex + codex}
    other = [item for item in above if item.route_id not in used]
    below = sorted(
        (item for item in present if item.capability_score_lower < floor),
        key=lambda item: (-item.capability_score_lower, item.route_id),
    )
    for values in (callable_non_codex, other, codex):
        values.sort(key=rank)
    counts = {
        "callable_non_codex": len(callable_non_codex),
        "other_above_floor": len(other),
        "codex_subscription_fallback": len(codex),
        "nearest_below_floor": len(below),
        "alias_routes_collapsed": collapsed,
    }
    execution_set = build_execution_set(
        catalogue,
        common_floor=floor,
        requirements=requirements or RoutingRequirements(),
        reasoning_effort=reasoning_effort,
        live_route_ids=live_route_ids,
    )
    eligible_ids = {item.route_id for item in execution_set.eligible}
    model_variants: dict[str, list[CatalogueRecommendationItem]] = {}
    for item in all_items:
        if item.live_present and item.capability_score_lower >= floor:
            model_variants.setdefault(item.equivalence_identity, []).append(item)
    model_groups = []
    for identity, variants in model_variants.items():
        variants.sort(
            key=lambda item: (
                -int(item.route_id in eligible_ids),
                -int(
                    "non_codex" in item.operator_resource_class
                    and item.responses_callability in _CALLABLE
                ),
                *rank(item),
            )
        )
        model_groups.append(
            CatalogueModelGroup(
                model_identity=identity,
                displayed_model=identity,
                route_count=len(variants),
                eligible_route_count=sum(item.route_id in eligible_ids for item in variants),
                configuration_count=len({item.reasoning_mode for item in variants}),
                configurations=variants,
            )
        )
    model_groups.sort(
        key=lambda group: (
            -int(group.eligible_route_count > 0),
            -int(
                any(
                    "non_codex" in item.operator_resource_class
                    and item.responses_callability in _CALLABLE
                    for item in group.configurations
                )
            ),
            -max(item.capability_score_lower for item in group.configurations),
            group.model_identity,
        )
    )
    counts["above_floor_models"] = len(model_groups)
    counts["above_floor_routes"] = sum(group.route_count for group in model_groups)
    counts["eligible_models"] = sum(group.eligible_route_count > 0 for group in model_groups)
    return CatalogueRecommendation(
        policy_version=str(catalogue.policy.get("policy_version") or "unknown"),
        forecast_version=str(catalogue.policy.get("policy_version") or "unknown"),
        forecast_hash=catalogue.hashes[REQUIRED_FILES[1]],
        readiness_version=str(catalogue.manifest.get("scanner_version") or "unknown"),
        readiness_hash=catalogue.hashes[REQUIRED_FILES[2]],
        source_snapshot_timestamp=str(
            catalogue.forecasts[0].get("source_snapshot_timestamp") or "unknown"
        ),
        readiness_observed_at=str(observed) if observed else None,
        raw_benchmark_floor=raw_floor,
        common_capability_floor=floor,
        total_route_count=len(catalogue.forecasts),
        live_present_count=sum(
            row["provider_model_route_id"] in live_route_ids for row in catalogue.forecasts
        ),
        section_counts=counts,
        callable_non_codex=callable_non_codex[:20],
        other_above_floor=other[:20],
        codex_subscription_fallback=codex[:20],
        nearest_below_floor=below[:10],
        execution_set=execution_set,
        model_groups=model_groups[:20],
        stale_warning=(
            "Readiness is a historical snapshot; current access is checked before use."
            if not readiness_is_recent(observed)
            else None
        ),
    )
