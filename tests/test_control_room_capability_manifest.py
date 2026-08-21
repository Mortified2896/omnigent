"""Completeness contract for intentional Control Room source deviations."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "docs/control-room-v0.10-capability-audit.json"
ALLOWED = {
    "RETAIN/REPLAY",
    "PORT SEMANTICALLY",
    "SOLVED UPSTREAM",
    "SUPERSEDED/OBSOLETE",
    "BLOCKED — OWNER DECISION REQUIRED",
}


def _audit() -> dict[str, object]:
    return json.loads(AUDIT.read_text())


def test_every_fork_only_commit_has_a_granular_reviewed_disposition() -> None:
    commits = _audit()["fork_only_commits"]
    assert len(commits) == 57
    assert len({entry["sha"] for entry in commits}) == 57
    for entry in commits:
        assert len(entry["sha"]) == 40
        assert entry["title"]
        assert entry["disposition"] in ALLOWED
        for field in (
            "capability",
            "current_consumer",
            "upstream_v0_10_implementation",
            "evidence",
            "destination",
            "validation_required",
            "risk_if_omitted",
        ):
            assert entry[field], f"{entry['sha']} lacks {field}"


def test_every_removed_fork_path_has_an_explicit_disposition() -> None:
    removed = _audit()["tree_completeness"]["fork_files_absent_from_candidate"]
    assert removed
    assert len({entry["path"] for entry in removed}) == len(removed)
    for entry in removed:
        assert entry["disposition"] in ALLOWED
        assert entry["destination"]
        assert entry["evidence"]


def test_retained_required_capabilities_still_exist() -> None:
    for capability in _audit()["required_capabilities"]:
        assert capability["disposition"] in ALLOWED
        assert capability["owner"]
        if capability["disposition"] == "RETAIN/REPLAY":
            assert capability["required_paths"]
            for path in capability["required_paths"]:
                assert (ROOT / path).exists(), f"required capability disappeared: {path}"
        elif not capability["required_paths"]:
            assert capability.get("merge_blocker"), (
                f"{capability['id']} has no source and is not an explicit merge blocker"
            )
