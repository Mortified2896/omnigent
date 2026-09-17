from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.server.o3_routing_review import tb4_floor_experiment as floor_module
from omnigent.server.o3_routing_review.models import (
    CatalogueExecutionDecision,
    CatalogueExecutionSet,
)
from omnigent.server.o3_routing_review.tb4_floor_experiment import (
    choose_arm,
    filter_execution_set_for_tb4,
)


def decision(
    *,
    route: str = "codex/gpt-test",
    model: str = "openai/gpt-test",
    reasoning: str = "default",
    exclusions: list[str] | None = None,
) -> CatalogueExecutionDecision:
    return CatalogueExecutionDecision(
        metadata={
            "forecast": {
                "canonical_live_model": model,
                "reasoning_mode": reasoning,
            }
        },
        route_id=route,
        provider_id=route.split("/", 1)[0],
        displayed_model=model.split("/", 1)[-1],
        reasoning_mode=reasoning,
        capability_score_lower=72.0,
        equivalence_identity=model,
        compatibility_basis=["fixture"],
        exclusions=exclusions or [],
    )


def execution_set(*rows: CatalogueExecutionDecision) -> CatalogueExecutionSet:
    eligible = [row for row in rows if not row.exclusions]
    excluded = [row for row in rows if row.exclusions]
    return CatalogueExecutionSet(
        total_evaluated=len(rows),
        eligible_count=len(eligible),
        eligible=eligible,
        excluded=excluded,
        exclusion_counts={},
    )


def baseline(score: float, *, model: str = "openai/gpt-test", effort: str = "high") -> dict:
    return {
        "canonical_model": model,
        "reasoning_effort": effort,
        "baseline_score": score,
        "baseline_percent": score * 100,
        "observation_count": 2,
        "harness_count": 2,
        "harnesses_observed": ["codex", "mini-swe-agent"],
        "source_families": ["artificial_analysis", "harbor"],
    }


def test_assignment_is_stable_and_uses_both_arms() -> None:
    observed = set()
    for index in range(100):
        first = choose_arm(f"task-{index}", 30.0, 50.0)
        second = choose_arm(f"task-{index}", 30.0, 50.0)
        assert first == second
        assert first[1] == 0.5
        assert first[2] in {30.0, 50.0}
        observed.add(first[0])
    assert observed == {"user", "adviser"}


def test_equal_floors_are_not_reported_as_randomized() -> None:
    assert choose_arm("same-task", 42.5, 42.5) == ("same", 1.0, 42.5)


def test_exact_tb4_gate_replaces_only_legacy_capability_floor() -> None:
    row = decision(
        exclusions=["conservative capability score 61 is below floor 80"]
    )
    result = filter_execution_set_for_tb4(
        execution_set(row),
        floor_score=0.35,
        requested_effort="high",
        baseline_index={("openai/gpt-test", "high"): baseline(0.40)},
    )
    assert result.eligible_count == 1
    assert result.eligible[0].exclusions == []
    assert result.eligible[0].metadata["tb4_baseline"]["baseline_score"] == 0.40
    assert result.eligible[0].capability_score_lower == 40.0


def test_exact_tb4_floor_excludes_model_below_floor() -> None:
    row = decision()
    result = filter_execution_set_for_tb4(
        execution_set(row),
        floor_score=0.45,
        requested_effort="high",
        baseline_index={("openai/gpt-test", "high"): baseline(0.40)},
    )
    assert result.eligible_count == 0
    assert result.excluded[0].exclusions == [
        "TB4 baseline 0.4 is below exact floor 0.45"
    ]


def test_structural_exclusions_are_never_removed_by_tb4_gate() -> None:
    row = decision(
        exclusions=[
            "conservative capability score 61 is below floor 80",
            "tool calling is not supported",
        ]
    )
    result = filter_execution_set_for_tb4(
        execution_set(row),
        floor_score=0.35,
        requested_effort="high",
        baseline_index={("openai/gpt-test", "high"): baseline(0.60)},
    )
    assert result.eligible_count == 0
    assert result.excluded[0].exclusions == ["tool calling is not supported"]


def test_default_route_uses_requested_reasoning_effort_baseline() -> None:
    row = decision(reasoning="default")
    result = filter_execution_set_for_tb4(
        execution_set(row),
        floor_score=0.30,
        requested_effort="xhigh",
        baseline_index={
            ("openai/gpt-test", "high"): baseline(0.20, effort="high"),
            ("openai/gpt-test", "xhigh"): baseline(0.50, effort="xhigh"),
        },
    )
    assert result.eligible_count == 1
    assert result.eligible[0].metadata["tb4_baseline"]["reasoning_effort"] == "xhigh"


def test_missing_model_effort_baseline_fails_closed() -> None:
    row = decision()
    result = filter_execution_set_for_tb4(
        execution_set(row),
        floor_score=0.01,
        requested_effort="high",
        baseline_index={},
    )
    assert result.eligible_count == 0
    assert result.excluded[0].exclusions == ["TB4 model/effort baseline is unavailable"]


def test_distinct_literal_floors_are_not_treated_as_equal() -> None:
    arm, propensity, chosen = choose_arm("near-equal", 32.0, 32.0 + 1e-10)
    assert arm in {"user", "adviser"}
    assert propensity == 0.5
    assert chosen in {32.0, 32.0 + 1e-10}


def test_empty_baseline_index_never_reads_an_ambient_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_load() -> dict:
        pytest.fail("An explicitly empty baseline index must remain empty")

    monkeypatch.setattr(floor_module, "_load_baseline", unexpected_load)
    result = filter_execution_set_for_tb4(
        execution_set(decision()),
        floor_score=0.0,
        requested_effort="high",
        baseline_index={},
    )
    assert result.eligible_count == 0


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.01, 40.0, True])
def test_invalid_baseline_scores_never_admit_a_route(score: float) -> None:
    result = filter_execution_set_for_tb4(
        execution_set(decision()),
        floor_score=0.0,
        requested_effort="high",
        baseline_index={("openai/gpt-test", "high"): baseline(score)},
    )
    assert result.eligible_count == 0
    assert result.excluded[0].metadata["tb4_baseline"] is None


def test_conflicting_score_units_fail_closed() -> None:
    row = baseline(0.4)
    row["baseline_percent"] = 0.4
    result = filter_execution_set_for_tb4(
        execution_set(decision()),
        floor_score=0.3,
        requested_effort="high",
        baseline_index={("openai/gpt-test", "high"): row},
    )
    assert result.eligible_count == 0


@pytest.mark.parametrize("floor", [float("nan"), float("inf"), -0.01, 40.0, True])
def test_gate_requires_a_finite_unit_interval_floor(floor: float) -> None:
    with pytest.raises(ValueError, match="floor"):
        filter_execution_set_for_tb4(
            execution_set(decision()),
            floor_score=floor,
            requested_effort="high",
            baseline_index={("openai/gpt-test", "high"): baseline(0.4)},
        )


def test_inferred_checkpoint_is_not_measured_identity() -> None:
    row = decision(route="opencode/opaque-test", model="opaque-test")
    row.metadata["forecast"]["inferred_base_checkpoint"] = "openai/gpt-test"
    row.metadata["forecast"]["estimate_method"] = "opaque_alias_hypothesis"
    result = filter_execution_set_for_tb4(
        execution_set(row),
        floor_score=0.3,
        requested_effort="high",
        baseline_index={("openai/gpt-test", "high"): baseline(0.8)},
    )
    assert result.eligible_count == 0


def test_conflicting_model_identity_matches_fail_closed() -> None:
    row = decision(route="codex/gpt-other")
    result = filter_execution_set_for_tb4(
        execution_set(row),
        floor_score=0.3,
        requested_effort="high",
        baseline_index={
            ("openai/gpt-test", "high"): baseline(0.8),
            ("gpt-other", "high"): baseline(0.2, model="openai/gpt-other"),
        },
    )
    assert result.eligible_count == 0


def _write_baseline_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> None:
    document = {
        "schema_version": 1,
        "policy_version": "tb4-model-effort-baseline-v1",
        "benchmark_id": "terminal-bench",
        "benchmark_version": "4.0.0",
        "slice_id": "tb4.overall",
        "score_scale": "0..1",
        "baselines": [baseline(0.4)],
        **overrides,
    }
    (tmp_path / floor_module.BASELINE_FILENAME).write_text(json.dumps(document))
    monkeypatch.setenv(floor_module.CATALOG_DIR_ENV, str(tmp_path))


def test_loader_accepts_the_versioned_tb4_overall_unit_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_baseline_document(tmp_path, monkeypatch)
    index = floor_module._load_baseline()
    assert index[("openai/gpt-test", "high")]["baseline_score"] == 0.4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("benchmark_id", "other-benchmark"),
        ("benchmark_version", "2.0.0"),
        ("slice_id", "tb4.other"),
        ("score_scale", "0..100"),
    ],
)
def test_loader_rejects_wrong_benchmark_or_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    _write_baseline_document(tmp_path, monkeypatch, **{field: value})
    with pytest.raises(ValueError, match="unsupported"):
        floor_module._load_baseline()


@pytest.mark.parametrize("effort", [None, "", "default", "unknown"])
def test_loader_does_not_invent_a_reasoning_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effort: str | None
) -> None:
    row = baseline(0.4)
    row["reasoning_effort"] = effort
    _write_baseline_document(tmp_path, monkeypatch, baselines=[row])
    assert floor_module._load_baseline() == {}
