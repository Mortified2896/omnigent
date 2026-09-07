import hashlib
import json
from pathlib import Path

import pytest

from omnigent.server.o3_routing_review.models import RoutingRequirements
from omnigent.server.o3_routing_review.recommendation import (
    REQUIRED_FILES,
    RecommendationCatalogue,
    load_recommendation_catalogue,
    recommend,
)


def _write(path: Path, value: object) -> str:
    raw = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _catalogue(tmp_path: Path) -> Path:
    policy = {
        "schema_version": 1,
        "policy_version": "test-policy-v1",
        "normalization": {
            "anchors": {"easy": 20, "normal": 40, "moderate": 60, "hard": 80, "frontier": 95}
        },
    }
    rows = [
        {
            "provider_model_route_id": "free/model-a",
            "provider": "free",
            "display_model_alias": "Model A",
            "canonical_live_model": "model-a",
            "inferred_base_checkpoint": "model-a",
            "reasoning_mode": "high",
            "capability_score_central": 90,
            "capability_score_lower": 81,
            "capability_score_upper": 96,
            "adviser_applicable": True,
            "confidence": "high",
            "estimated_tier": "frontier",
            "conservative_tier": "hard",
            "estimate_method": "fixture",
            "cost_class": "unknown",
            "source_snapshot_timestamp": "2026-01-01T00:00:00Z",
        },
        {
            "provider_model_route_id": "alias/model-a",
            "provider": "alias",
            "display_model_alias": "Model A alias",
            "canonical_live_model": "model-a",
            "inferred_base_checkpoint": "model-a",
            "reasoning_mode": "high",
            "capability_score_central": 90,
            "capability_score_lower": 81,
            "adviser_applicable": True,
            "confidence": "high",
            "estimated_tier": "frontier",
            "conservative_tier": "hard",
            "estimate_method": "fixture",
            "cost_class": "free",
            "source_snapshot_timestamp": "2026-01-01T00:00:00Z",
        },
        {
            "provider_model_route_id": "codex/model-b",
            "provider": "codex",
            "display_model_alias": "Model B",
            "canonical_live_model": "model-b",
            "reasoning_mode": "default",
            "capability_score_central": 99,
            "capability_score_lower": 79,
            "adviser_applicable": True,
            "confidence": "medium",
            "estimated_tier": "frontier",
            "conservative_tier": "moderate",
            "estimate_method": "fixture",
            "cost_class": "paid",
            "source_snapshot_timestamp": "2026-01-01T00:00:00Z",
        },
    ]
    readiness_rows = [
        {
            "route_id": "free/model-a",
            "equivalence_key": "model-a",
            "responses_callability": "callable_now",
            "operator_resource_policy": "non_codex_zero_marginal_cost",
        },
        {
            "route_id": "alias/model-a",
            "equivalence_key": "model-a",
            "responses_callability": "auth_failed",
            "operator_resource_policy": "non_codex_zero_marginal_cost",
        },
        {
            "route_id": "codex/model-b",
            "equivalence_key": "model-b",
            "responses_callability": "codex_excluded",
            "operator_resource_policy": "codex_subscription",
        },
    ]
    _write(tmp_path / REQUIRED_FILES[0], policy)
    forecast_hash = _write(
        tmp_path / REQUIRED_FILES[1], {"schema_version": 1, "route_count": 3, "rows": rows}
    )
    readiness_hash = _write(
        tmp_path / REQUIRED_FILES[2],
        {"schema_version": 1, "route_count": 3, "rows": readiness_rows},
    )
    _write(
        tmp_path / REQUIRED_FILES[3],
        {
            "schema_version": 1,
            "route_count": 3,
            "scanner_version": "test",
            "live_catalogue_timestamp": "2026-01-02T00:00:00Z",
            "input_artifact_hashes": {REQUIRED_FILES[1]: forecast_hash},
            "output_hashes": {REQUIRED_FILES[2]: readiness_hash},
        },
    )
    return tmp_path


def test_loader_validates_and_recommendation_uses_conservative_floor(tmp_path: Path) -> None:
    catalogue = load_recommendation_catalogue(str(_catalogue(tmp_path)))
    result = recommend(
        catalogue,
        difficulty="hard",
        raw_floor=0.3,
        live_route_ids={"free/model-a", "alias/model-a", "codex/model-b"},
    )
    assert result.total_route_count == 3
    assert result.common_capability_floor == 80
    assert [item.route_id for item in result.callable_non_codex] == ["free/model-a"]
    assert result.callable_non_codex[0].alternate_route_ids == ["alias/model-a"]
    assert not result.codex_subscription_fallback
    assert result.nearest_below_floor[0].route_id == "codex/model-b"


def test_reasoning_modes_are_not_collapsed_and_payload_is_bounded(tmp_path: Path) -> None:
    catalogue = load_recommendation_catalogue(str(_catalogue(tmp_path)))
    result = recommend(
        catalogue,
        difficulty="easy",
        raw_floor=0.1,
        live_route_ids=set(catalogue.readiness),
    )
    assert all(
        len(section) <= 20
        for section in (
            result.callable_non_codex,
            result.other_above_floor,
            result.codex_subscription_fallback,
        )
    )
    assert len(result.nearest_below_floor) <= 10
    assert len(result.model_dump_json()) < 100_000


def test_missing_files_and_manifest_hash_fail_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="OMNIGENT_O3_RECOMMENDATION_CATALOG_DIR"):
        load_recommendation_catalogue(str(tmp_path))
    root = _catalogue(tmp_path)
    manifest = json.loads((root / REQUIRED_FILES[3]).read_text())
    manifest["input_artifact_hashes"][REQUIRED_FILES[1]] = "0" * 64
    _write(root / REQUIRED_FILES[3], manifest)
    load_recommendation_catalogue.cache_clear()
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        load_recommendation_catalogue(str(root))


def test_execution_set_is_uncapped_and_checks_effective_io_per_route() -> None:
    def row(route_id: str, **updates: object) -> dict[str, object]:
        value: dict[str, object] = {
            "provider_model_route_id": route_id,
            "provider": route_id.split("/", 1)[0],
            "display_model_alias": route_id,
            "reasoning_mode": "high",
            "capability_score_central": 90,
            "capability_score_lower": 85,
            "adviser_applicable": True,
            "confidence": "high",
            "estimated_tier": "frontier",
            "conservative_tier": "hard",
            "estimate_method": "fixture",
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "context_window": 128_000,
            "tool_calling": True,
            "responses_compatibility": "catalog_declared",
            "source_snapshot_timestamp": "2026-01-01T00:00:00Z",
        }
        value.update(updates)
        return value

    eligible_rows = [row(f"provider-{index}/model-{index}") for index in range(25)]
    eligible_rows.extend(
        [
            row(
                "translated/model",
                responses_compatibility="unknown",
                runtime_compatibility="proven",
            ),
            row("alias-a/model", reasoning_mode="default"),
            row("alias-b/model", reasoning_mode="default"),
        ]
    )
    incompatible_rows = [
        row("bad/output", output_modalities=["image"]),
        row("bad/tools", tool_calling=False),
        row("bad/context", context_window=None),
        row("bad/endpoint", responses_compatibility="unknown"),
        row("bad/reasoning", reasoning_mode="low"),
        row("bad/floor", capability_score_lower=79),
        row("bad/not-live"),
    ]
    rows = eligible_rows + incompatible_rows
    readiness = {
        item["provider_model_route_id"]: {
            "route_id": item["provider_model_route_id"],
            **(
                {"equivalence_key": "evidenced-alias"}
                if str(item["provider_model_route_id"]).startswith("alias-")
                else {}
            ),
        }
        for item in rows
    }
    catalogue = RecommendationCatalogue(
        policy={
            "policy_version": "test-policy-v1",
            "normalization": {"anchors": {"hard": 80}},
        },
        forecasts=tuple(rows),
        readiness=readiness,
        manifest={},
        hashes={REQUIRED_FILES[1]: "forecast", REQUIRED_FILES[2]: "readiness"},
    )
    live = ({str(item["provider_model_route_id"]) for item in rows} - {"bad/not-live"}) | {
        "live/unrepresented"
    }
    result = recommend(
        catalogue,
        difficulty="hard",
        raw_floor=0.4,
        live_route_ids=live,
        requirements=RoutingRequirements(
            tools=True,
            vision=True,
            input_modalities=["text", "image"],
            output_modalities=["text"],
            minimum_context_tokens=32_000,
        ),
        reasoning_effort="high",
    )

    assert len(result.other_above_floor) == 20
    assert result.execution_set is not None
    assert result.execution_set.total_evaluated == len(rows) + 1
    assert result.execution_set.eligible_count == 28
    assert len(result.execution_set.eligible) == 28
    assert "provider-24/model-24" in {item.route_id for item in result.execution_set.eligible}
    assert {"alias-a/model", "alias-b/model"} <= {
        item.route_id for item in result.execution_set.eligible
    }
    assert {item.route_id: item.exclusions for item in result.execution_set.excluded} == {
        "bad/context": ["input/context limit is unknown"],
        "bad/endpoint": ["Responses client-contract compatibility is unresolved"],
        "bad/floor": ["conservative capability score 79 is below floor 80"],
        "bad/not-live": ["route is not in the current exposed catalogue"],
        "bad/output": ["missing required output modalities: text"],
        "bad/reasoning": ["reasoning configuration 'low' does not satisfy approved 'high'"],
        "bad/tools": ["tool calling is not supported"],
        "live/unrepresented": [
            "conservative capability evidence is unavailable",
            "input modalities are unknown",
            "output modalities are unknown",
            "input/context limit is unknown",
            "tool calling is not supported",
            "Responses client-contract compatibility is unresolved",
        ],
    }


def test_unresolved_structured_output_and_output_limit_fail_closed() -> None:
    row = {
        "provider_model_route_id": "provider/model",
        "provider": "provider",
        "reasoning_mode": "default",
        "capability_score_central": 90,
        "capability_score_lower": 90,
        "adviser_applicable": True,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "context_window": 128_000,
        "tool_calling": True,
        "responses_compatibility": "proven",
    }
    catalogue = RecommendationCatalogue(
        policy={"normalization": {"anchors": {"easy": 20}}},
        forecasts=(row,),
        readiness={},
        manifest={},
        hashes={REQUIRED_FILES[1]: "forecast", REQUIRED_FILES[2]: "readiness"},
    )
    result = recommend(
        catalogue,
        difficulty="easy",
        raw_floor=0.1,
        live_route_ids={"provider/model"},
        requirements=RoutingRequirements(
            structured_output=True,
            minimum_output_tokens=4_096,
        ),
    )

    assert result.execution_set is not None
    assert result.execution_set.eligible_count == 0
    assert result.execution_set.excluded[0].exclusions == [
        "output limit is unknown",
        "structured output support is unresolved",
    ]
