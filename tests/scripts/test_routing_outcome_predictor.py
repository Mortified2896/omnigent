import hashlib
import json
from datetime import datetime, timezone

import pytest

from scripts.routing_outcome_predictor import (
    fit,
    join,
    losses,
    prepare,
    probabilities,
    splits,
    vector,
)
from scripts.routing_prediction_dataset import AXES, canonical_hash


def row(case, group=None, date="2026-10-01T00:00:00Z", **changes):
    return {
        "case_id": case,
        "task_group": group or case,
        "dispatch_timestamp": date,
        "shard": "a",
        "recorded_model": "m",
        "recorded_effort": "max",
        "harness": "codex",
        "difficulty": {"axes": dict.fromkeys(AXES, 1), "task_type": "coding"},
        "public_scores": {"fixture": {"score": 50}},
        "target": {"outcome": "success"},
        **changes,
    }


def test_standard_library_softmax_learns_without_zeroing_unobserved_failure():
    x = [[1.0, -1.0]] * 12 + [[1.0, 1.0]] * 12
    model = fit(x, [1] * 12 + [0] * 12, accelerated=False)
    assert model["converged"]
    assert probabilities(model["weights"], [1, 1])[0] > probabilities(model["weights"], [1, -1])[0]
    for values in ([1, 1], [1, -1]):
        p = probabilities(model["weights"], values)
        assert sum(p) == pytest.approx(1)
        assert all(0 < v < 1 for v in p)
    assert model["calibrated"] is False


def test_scaling_and_missing_value_imputation_use_training_only():
    a, b = row("a"), row("b")
    b["difficulty"]["axes"] = dict.fromkeys(AXES, 3)
    prep = prepare([a, b], None)
    extreme = row("future", difficulty={"axes": dict.fromkeys(AXES, 30), "task_type": "coding"})
    assert prep["means"][0] == 2
    assert vector(extreme, prep)[1] == 28
    unknown = row(
        "unknown", difficulty={"axes": dict.fromkeys(AXES, None), "task_type": "unknown"}
    )
    assert vector(unknown, prep)[1] == 0
    assert prepare([a, b], None) == prep


def test_profile_and_project_holdouts_keep_shared_task_groups_out_of_training():
    rows = [
        row("a", group="shared"),
        row("b", group="shared", recorded_model="n", shard="b"),
        row("c", recorded_model="n", shard="b"),
    ]
    cutoff = datetime(2026, 10, 2, tzinfo=timezone.utc)
    for method in ("project_family", "model_effort_profile"):
        for _, training, test in splits(rows, method, cutoff):
            assert not {r["task_group"] for r in training} & {r["task_group"] for r in test}


def test_chronological_holdout_omits_groups_crossing_the_boundary():
    rows = [
        row("a", group="shared"),
        row("b", group="shared", date="2026-10-03T00:00:00Z"),
        row("c"),
        row("d", date="2026-10-03T00:00:00Z"),
    ]
    _, train, test = splits(rows, "chronological", datetime(2026, 10, 2, tzinfo=timezone.utc))[0]
    assert [r["case_id"] for r in train] == ["c"]
    assert [r["case_id"] for r in test] == ["d"]


def test_multiclass_losses_have_independent_expected_values():
    result = losses([0.5, 0.25, 0.25], "success")
    assert result["log_loss"] == pytest.approx(0.6931471805599453)
    assert result["brier"] == pytest.approx(0.375)


@pytest.fixture
def feature_collection(tmp_path):
    manifest, episodes, ratings = {"packet_files": {}}, [], []
    judge = {"configured_model": "fixture-judge", "configured_reasoning": "max"}
    for number in (1, 2):
        case = str(number)
        packet = {
            "case_id": case,
            "task_request": {"index": 5, "text": "fixture request"},
            "context_available_before_request": [{"index": 3, "text": "earlier context"}],
        }
        path = tmp_path / f"difficulty-packets-{number}.json"
        path.write_text(json.dumps([packet]))
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["packet_files"][path.name] = file_hash
        rating = {
            "case_id": case,
            "judge_model": judge["configured_model"],
            "judge_effort": judge["configured_reasoning"],
            "rubric_version": "hindsight-evaluator-v1-difficulty",
            "input_packet_sha256": canonical_hash(packet),
            "input_packet_file_sha256": file_hash,
            "human_validated": False,
            "routing_training_eligible": False,
            "axes": dict.fromkeys(AXES, 1),
            "task_type": "coding",
            "overall_band": "moderate",
            "confidence": 0.8,
            "preexecution_evidence_indices": [3, 5],
        }
        (tmp_path / f"difficulty-review-{number}.jsonl").write_text(json.dumps(rating) + "\n")
        episodes.append(row(case))
        ratings.append(rating)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return episodes, tmp_path, {"profiles": []}, judge, ratings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rubric_version", "changed-rubric"),
        ("preexecution_evidence_indices", [6]),
        ("routing_training_eligible", True),
        ("confidence", float("nan")),
    ],
)
def test_feature_join_rejects_changed_or_leaking_judgments(feature_collection, field, value):
    episodes, directory, catalog, judge, ratings = feature_collection
    assert len(join(episodes, directory, catalog, judge)) == 2
    ratings[0][field] = value
    (directory / "difficulty-review-1.jsonl").write_text(json.dumps(ratings[0]) + "\n")
    with pytest.raises(ValueError):
        join(episodes, directory, catalog, judge)


def test_feature_join_binds_original_packet_file_and_complete_case_set(feature_collection):
    episodes, directory, catalog, judge, _ = feature_collection
    path = directory / "difficulty-packets-1.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="Frozen difficulty packet file changed"):
        join(episodes, directory, catalog, judge)


def test_feature_join_rejects_duplicate_missing_score_crosswalk(feature_collection):
    episodes, directory, catalog, judge, _ = feature_collection
    missing = {
        "match_basis": "documented_model_and_effort",
        "trace_model": "m",
        "trace_effort": "max",
        "benchmark": "fixture",
        "score": None,
    }
    catalog["profiles"] = [missing, missing]
    with pytest.raises(ValueError, match="Ambiguous score crosswalk"):
        join(episodes, directory, catalog, judge)


def test_predictor_rejects_empty_rows_or_zero_iterations():
    for x, options in (([[]], {}), ([[1.0]], {"maximum_iterations": 0})):
        with pytest.raises(ValueError, match="Invalid predictor fitting inputs"):
            fit(x, [0], **options)
