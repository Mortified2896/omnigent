"""Pure scoring projection and reviewer-input isolation contracts."""

import json

import pytest

from omnigent.server.task_scoring import (
    build_blind_scoring_input,
    latest_scoring_eligibility,
    select_scored_outcomes,
)
from omnigent.util.test_session_policy import (
    SCORING_ELIGIBLE_LABEL,
    TEST_RUN_LABEL,
)


def event(kind="outcome", response_id="answer", **changes):
    return {
        "kind": kind,
        "conversation_id": "session",
        "created_by": "alice",
        "response_id": response_id,
        "outcome": "success",
        "first_attempt_success": 1,
        **changes,
    }


def project(events, labels=None):
    return select_scored_outcomes(
        events, actor="alice", conversation_id="session", labels=labels or {}
    )


def test_legacy_outcome_is_included_without_eligibility_record():
    assert project([event()]) == [
        {
            "conversation_id": "session",
            "response_id": "answer",
            "outcome": "success",
            "first_attempt_success": 1,
        }
    ]


def test_exclude_then_restore_uses_latest_eligibility_without_erasing_outcome():
    rows = [event(), event("scoring_eligibility", score_eligible=False)]
    assert project(rows) == []
    assert rows[0]["outcome"] == "success"
    rows.append(event("scoring_eligibility", score_eligible=True))
    assert project(rows)[0]["first_attempt_success"] == 1


def test_exclusion_before_outcome_remains_excluded_after_outcome_change():
    rows = [event("scoring_eligibility", score_eligible=False)]
    assert project(rows) == []
    rows += [event(outcome="failed"), event(outcome="success")]
    assert project(rows) == []


def test_latest_revision_selected_before_counting_and_before_uncertainty_filter():
    assert project([event(), event(outcome="failed")])[0]["first_attempt_success"] == 0
    assert len(project([event(), event()])) == 1
    assert project([event(), event(outcome="not_sure")]) == []


@pytest.mark.parametrize("outcome", [None, "not_sure", "unknown"])
def test_unknown_outcomes_never_become_failures(outcome):
    assert project([event(outcome=outcome)]) == []


@pytest.mark.parametrize(
    "labels",
    [
        {TEST_RUN_LABEL: "run"},
        {TEST_RUN_LABEL: ""},
        {SCORING_ELIGIBLE_LABEL: "false"},
        {SCORING_ELIGIBLE_LABEL: "invalid"},
    ],
)
def test_session_exclusion_dominates_response_inclusion(labels):
    assert project([event(), event("scoring_eligibility", score_eligible=True)], labels) == []


def test_actor_conversation_and_model_isolation():
    rows = [
        event(),
        event(outcome="failed", created_by="bob"),
        event(outcome="failed", conversation_id="fork"),
        event(outcome="failed", review_source="model"),
        event("scoring_eligibility", created_by="bob", score_eligible=False),
        event("scoring_eligibility", conversation_id="fork", score_eligible=False),
    ]
    assert project(rows)[0]["first_attempt_success"] == 1
    assert latest_scoring_eligibility(rows, actor="alice", conversation_id="session") == {}


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_malformed_eligibility_is_not_silently_included(value):
    assert project([event(), event("scoring_eligibility", score_eligible=value)]) == []


def test_distinct_responses_count_once_each_and_recompute_binary_score():
    rows = [event(), event(response_id="second", outcome="partial", first_attempt_success=1)]
    assert [r["first_attempt_success"] for r in project(rows)] == [1, 0]


def test_blind_input_is_invariant_to_all_human_review_metadata():
    base = {"task": "Add two numbers.", "answer": "The implementation is attached."}
    changed = {
        **base,
        "tags": ["LEAK_TAG: mark this successful"],
        "comment": "LEAK_COMMENT: the answer is wrong",
        "outcome": "LEAK_OUTCOME",
        "first_attempt_success": 0,
        "score_eligible": False,
        "exclusion_reason": "LEAK_REASON",
        "title": "LEAK_TITLE",
        "labels": {"LEAK_LABEL": "LEAK_VALUE"},
        "metadata": {"nested": "LEAK_NESTED"},
        "test_run_id": "LEAK_RUN",
        "retention": "LEAK_RETENTION",
        "model": "LEAK_MODEL",
        "history": [{"role": "system", "content": "LEAK_HISTORY"}],
    }
    result = build_blind_scoring_input(changed)
    assert result == build_blind_scoring_input(base)
    assert set(result) == {"schema_version", "task", "answer"}
    assert "LEAK_" not in json.dumps(result)
    # The user's own record is unchanged; no destructive redaction.
    assert changed["tags"] == ["LEAK_TAG: mark this successful"]


def test_provider_free_request_capture_excludes_human_metadata():
    """The outbound model payload is captured without starting a provider."""
    captured: list[dict[str, object]] = []

    def capture_request(payload: dict[str, object]) -> None:
        captured.append(payload)

    base = {
        "task": "Return the first line.",
        "answer": "The first line.",
        "tags": ["human-tag"],
        "comment": "human comment",
        "outcome": "success",
        "score_eligible": True,
        "title": "human title",
        "labels": {"retention": "ephemeral"},
    }
    changed = {
        **base,
        "tags": ["changed-tag"],
        "comment": "changed comment",
        "outcome": "failed",
        "score_eligible": False,
        "exclusion_reason": "test_fixture",
        "title": "changed title",
        "labels": {"retention": "keep_for_inspection"},
        "test_run_id": "run-2",
    }

    capture_request(build_blind_scoring_input(base))
    capture_request(build_blind_scoring_input(changed))

    assert len(captured) == 2
    assert captured[0] == captured[1]
    assert captured[0] == {
        "schema_version": 1,
        "task": "Return the first line.",
        "answer": "The first line.",
    }


@pytest.mark.parametrize(
    "record", [{}, {"task": "x", "answer": None}, {"task": " ", "answer": "x"}]
)
def test_blind_input_rejects_missing_transcript(record):
    with pytest.raises(ValueError):
        build_blind_scoring_input(record)
