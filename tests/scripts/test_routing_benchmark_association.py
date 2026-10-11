import pytest

from scripts.routing_benchmark_association import associations, profile_summary, spearman


def row(model="m", effort="low", outcome="success", **extra):
    return {
        "recorded_model": model,
        "recorded_effort": effort,
        "outcome": outcome,
        "confidence": "high",
        "evidence_profile_mismatch": False,
        "shard": "project",
        "timestamp": "2026-10-11T00:00:00Z",
        **extra,
    }


def score(model="m", effort="low", value=50, **extra):
    return {
        "trace_model": model,
        "trace_effort": effort,
        "score": value,
        "benchmark": "fixture",
        "version": "1",
        "source_url": "https://example.org/benchmark",
        "published_profile_id": f"{model}-{effort}",
        "match_basis": "documented_model_and_effort",
        **extra,
    }


def test_missing_effort_and_estimated_scores_do_not_become_zero_or_model_only_matches():
    rows = [row(), row(effort="max"), row(model="missing"), row(model="estimated")]
    catalog = {"profiles": [score(value=None), score(model="estimated", estimated=True)]}
    result = associations(rows, catalog)[0]
    assert result["model_effort_matched_proposals"] == 0
    assert result["missing"] == {
        "published_score_missing": 1,
        "no_documented_model_effort_match": 2,
        "estimated_public_score": 1,
    }
    assert result["predictive_validation"] is False
    assert result["causal_completion_improvement"] is None


def test_uncertain_and_mixed_reviews_are_reported_as_sensitivity_exclusions():
    rows = [
        row(),
        row(outcome="partial"),
        row(outcome="not_sure"),
        row(evidence_profile_mismatch=True),
    ]
    result = associations(rows, {"profiles": [score()]})[0]
    assert result["model_effort_matched_proposals"] == 4
    all_known, stricter = result["sensitivities"]
    assert all_known["points"][0]["proposal_completion_rate"] == pytest.approx(2 / 3)
    assert stricter["points"][0]["proposal_completion_rate"] == 0.5
    assert all_known["thresholds"][0]["descriptive_profile_spearman"] is None


def test_repeated_tasks_do_not_become_independent_correlation_points():
    rows = [row()] * 50 + [row(model="n", outcome="partial")] * 50
    result = associations(rows, {"profiles": [score(), score(model="n", value=80)]})[0]
    threshold = result["sensitivities"][0]["thresholds"][0]
    assert threshold["proposals"] == 100
    assert threshold["model_effort_profiles"] == 2
    assert threshold["descriptive_profile_spearman"] is None


def test_duplicate_crosswalk_fails_instead_of_selecting_the_best_score():
    with pytest.raises(ValueError, match="Ambiguous"):
        associations([row()], {"profiles": [score(), score(value=90)]})


def test_profile_rates_and_tied_rank_correlation_have_independent_expected_values():
    points = profile_summary([row(), row(outcome="partial"), row(outcome="not_sure")])
    assert points[0]["known_proposals"] == 2
    assert points[0]["proposal_completion_rate"] == 0.5
    assert spearman([1, 2, 2, 4], [4, 3, 3, 1]) == pytest.approx(-1)
    assert spearman([1, 1, 1], [1, 2, 3]) is None
