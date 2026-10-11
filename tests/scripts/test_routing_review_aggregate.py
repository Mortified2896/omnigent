import hashlib
import json

import pytest

from scripts.routing_review_aggregate import aggregate, digest, publish


def fixture(tmp_path):
    item = {"kind": "assistant", "channel": "final", "text": "done", "turn_id": "t"}
    snapshot = tmp_path / "snapshot.jsonl"
    snapshot.write_text(json.dumps({"index": 0, **item}) + "\n")
    entry = {
        "source_id": "codex:fixture",
        "shard": "fixture",
        "checkpoint": 0,
        "snapshot": str(snapshot),
        "format": "jsonl",
        "strip_fields": ["index"],
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
    }
    record = {
        "source": "codex",
        "source_id": "fixture",
        "checkpoint": 0,
        "checkpoint_digest": digest(item),
        "turn_id": "t",
        "review_source": "assistant_proposal",
        "human_reviewed": False,
        "score_eligible": False,
        "outcome": "partial",
        "confidence": "medium",
        "reason": "Acceptance still missing",
        "scope": "latest request",
        "coverage": "One answer inspected",
        "required_capabilities": ["repo_change"],
        "evidence": [{"index": 0, "label": "answer"}],
    }
    path = tmp_path / "reviews.jsonl"
    path.write_text(json.dumps(record) + "\n")
    manifest = {"entries": [entry], "review_journals": [str(path)]}
    return item, entry, record, path, manifest


def test_append_revisions_count_once_and_remain_unvalidated(tmp_path):
    _, _, record, path, manifest = fixture(tmp_path)
    with path.open("a") as handle:
        handle.write(json.dumps({**record, "outcome": "success"}) + "\n")
    report, proposals = aggregate(manifest)
    assert report["reviewed_checkpoints"] == 1
    assert report["proposal_outcomes"] == {"success": 1}
    assert report["human_validated_labels"] == 0
    assert report["routing_training_eligible_labels"] == 0
    assert report["benchmark_predictive_correlation"] is None
    assert proposals[0]["source_id"] == "codex:fixture"
    destination = tmp_path / "proposals.jsonl"
    assert publish(destination, proposals, report["manifest_digest"]) == 1
    assert publish(destination, proposals, report["manifest_digest"]) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"checkpoint_digest": "wrong"},
        {"score_eligible": True},
        {"review_source": "human"},
        {"evidence": [{"index": 5}]},
        {"pending_evidence": [{"index": 0}]},
        {"pending_evidence": [{"index": 5}]},
    ],
)
def test_unbound_or_human_records_cannot_enter_assistant_proposal_queue(tmp_path, change):
    _, _, record, path, manifest = fixture(tmp_path)
    path.write_text(json.dumps({**record, **change}) + "\n")
    report, proposals = aggregate(manifest)
    assert report["reviewed_checkpoints"] == 0
    assert report["pending"] == 1
    assert len(report["invalid"]) == 1
    assert proposals == []


def test_later_owner_requests_remain_separate_from_checkpoint_evidence(tmp_path):
    _, entry, record, path, manifest = fixture(tmp_path)
    snapshot = tmp_path / "snapshot.jsonl"
    with snapshot.open("a") as handle:
        handle.write(json.dumps({"index": 1, "kind": "user", "text": "next task"}) + "\n")
    entry["snapshot_sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    record["pending_evidence"] = [{"index": 1, "label": "Unanswered next task"}]
    path.write_text(json.dumps(record) + "\n")
    report, proposals = aggregate(manifest)
    assert report["invalid"] == []
    assert proposals[0]["evidence"] == [{"index": 0, "label": "answer"}]
    assert proposals[0]["pending_evidence"] == record["pending_evidence"]


def test_missing_excluded_and_pending_do_not_become_failed_outcomes(tmp_path):
    _, entry, _, _, manifest = fixture(tmp_path)
    manifest["entries"] += [
        {"source_id": "codex:missing", "shard": "fixture", "checkpoint": None},
        {
            "source_id": "codex:test",
            "shard": "fixture",
            "checkpoint": None,
            "excluded_reason": "test_session",
        },
        {**entry, "source_id": "codex:pending"},
    ]
    report, _ = aggregate(manifest)
    assert report["inventoried_roots_or_sessions"] == 4
    assert report["reviewed_checkpoints"] == 1
    assert report["missing_checkpoint"] == 1
    assert report["excluded"] == {"test_session": 1}
    assert report["pending"] == 1
    assert report["proposal_outcomes"] == {"partial": 1}
