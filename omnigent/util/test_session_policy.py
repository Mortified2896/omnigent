"""Explicit live-test provenance and a non-destructive cleanup planner.

Labels are hygiene metadata, not an authorization boundary. The executor must
also use an authenticated owner API and its private creation manifest.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

TEST_RUN_LABEL = "omnigent.test.run_id"
TEST_CREATOR_LABEL = "omnigent.test.created_by"
TEST_RETENTION_LABEL = "omnigent.test.retention"
TEST_REASON_LABEL = "omnigent.test.keep_reason"
SCORING_ELIGIBLE_LABEL = "omnigent.scoring.eligible"


def test_session_labels(run_id: str, creator: str) -> dict[str, str]:
    """Stamp these at creation, before sending any test prompt."""
    if not run_id.strip() or not creator.strip():
        raise ValueError("Test run ID and creator must be non-empty")
    return {
        TEST_RUN_LABEL: run_id,
        TEST_CREATOR_LABEL: creator,
        TEST_RETENTION_LABEL: "ephemeral",
        SCORING_ELIGIBLE_LABEL: "false",
    }


def keep_for_inspection_labels(reason: str) -> dict[str, str]:
    """Merge this patch; do not replace the session's other labels."""
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise ValueError("Inspection reason must contain 1 to 1000 characters")
    return {
        TEST_RETENTION_LABEL: "keep_for_inspection",
        TEST_REASON_LABEL: reason,
        SCORING_ELIGIBLE_LABEL: "false",
    }


def session_score_eligible(labels: Mapping[str, str]) -> bool:
    """Legacy sessions default to included; any explicit test marker excludes."""
    return TEST_RUN_LABEL not in labels and labels.get(SCORING_ELIGIBLE_LABEL, "true") == "true"


@dataclass(frozen=True)
class TestCleanupDecision:
    action: Literal["preserve", "keep_for_inspection", "delete_candidate"]
    reason: str


def plan_test_cleanup(
    session: Mapping[str, object],
    *,
    run_id: str,
    creator: str,
    created_session_ids: Collection[str],
    verified_result: Literal["passed", "failed", "unexpected"] | None,
    unchanged_since_verification: bool,
) -> TestCleanupDecision:
    """Plan only. Never infer test ownership from a title, age, or chat text.

    A delete_candidate is NOT deletion authorization. Before deletion the caller
    must prove no concurrent user edits, descendants, pins, or active work would
    be lost. Without a conditional deletion contract, preserve/archive instead.
    """
    session_id = session.get("id")
    labels = session.get("labels")
    if (
        not run_id.strip()
        or not creator.strip()
        or not isinstance(session_id, str)
        or session_id not in created_session_ids
        or not isinstance(labels, dict)
        or labels.get(TEST_RUN_LABEL) != run_id
        or labels.get(TEST_CREATOR_LABEL) != creator
    ):
        return TestCleanupDecision("preserve", "Not proven to belong to this test run")
    if labels.get(TEST_RETENTION_LABEL) == "keep_for_inspection":
        return TestCleanupDecision("keep_for_inspection", "Explicit inspection hold")
    if labels.get(TEST_RETENTION_LABEL) != "ephemeral":
        return TestCleanupDecision("preserve", "Missing or unknown retention policy")
    if labels.get(SCORING_ELIGIBLE_LABEL) != "false":
        return TestCleanupDecision("preserve", "Test scoring policy changed")
    if any(
        (key == "omnigent.pinned" or key.startswith("omnigent.pinned."))
        and value not in ("false", "0", "")
        for key, value in labels.items()
    ):
        return TestCleanupDecision("preserve", "Pinned session")
    if not unchanged_since_verification:
        return TestCleanupDecision("preserve", "Session changed or freshness is unknown")
    if session.get("status") not in ("idle", "completed", "failed"):
        return TestCleanupDecision("preserve", "Session is active or status is unknown")
    if session.get("parent_session_id") or session.get("has_children") is not False:
        return TestCleanupDecision("preserve", "Session tree is not proven isolated")
    if verified_result != "passed" or session.get("status") == "failed":
        return TestCleanupDecision(
            "keep_for_inspection", "Failure, unexpected result, or unverified run"
        )
    return TestCleanupDecision("delete_candidate", "Verified disposable test created by this run")
