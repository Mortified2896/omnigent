"""A test title alone can never authorize deletion."""

import pytest

from omnigent.util.test_session_policy import (
    SCORING_ELIGIBLE_LABEL,
    TEST_CREATOR_LABEL,
    TEST_RETENTION_LABEL,
    TEST_RUN_LABEL,
    keep_for_inspection_labels,
    plan_test_cleanup,
    session_score_eligible,
    test_session_labels as make_test_labels,
)


def session(**changes):
    return {
        "id": "mine", "title": "PONG test", "status": "idle",
        "labels": make_test_labels("run", "codex"), "has_children": False,
        **changes,
    }


def plan(value, **changes):
    kwargs = {
        "run_id": "run", "creator": "codex", "created_session_ids": {"mine"},
        "verified_result": "passed", "unchanged_since_verification": True,
        **changes,
    }
    return plan_test_cleanup(value, **kwargs)


def test_new_test_sessions_and_retained_evidence_are_always_excluded():
    labels = make_test_labels("run", "codex")
    assert not session_score_eligible(labels)
    labels.update(keep_for_inspection_labels("Inspect the regression"))
    assert not session_score_eligible(labels)
    assert plan(session(labels=labels)).action == "keep_for_inspection"


def test_only_manifest_owned_verified_disposable_session_is_a_candidate():
    assert plan(session()).action == "delete_candidate"
    assert plan(session(), created_session_ids=set()).action == "preserve"
    assert plan(session(), run_id="another").action == "preserve"
    assert plan(session(), creator="zcode").action == "preserve"


@pytest.mark.parametrize(
    "removed",
    [TEST_RUN_LABEL, TEST_CREATOR_LABEL, TEST_RETENTION_LABEL, SCORING_ELIGIBLE_LABEL],
)
def test_missing_marker_prevents_cleanup(removed):
    value = session()
    del value["labels"][removed]
    assert plan(value).action == "preserve"


@pytest.mark.parametrize("status", ["running", "waiting", "queued", "unknown", None])
def test_never_cleans_active_or_unknown_status(status):
    assert plan(session(status=status)).action == "preserve"


@pytest.mark.parametrize("result", ["failed", "unexpected", None])
def test_failures_and_unverified_tests_are_kept_for_inspection(result):
    assert plan(session(), verified_result=result).action == "keep_for_inspection"


def test_failed_session_cannot_be_deleted_by_stale_pass_result():
    assert plan(session(status="failed")).action == "keep_for_inspection"


@pytest.mark.parametrize("key", ["omnigent.pinned", "omnigent.pinned.alice"])
def test_human_pin_protects_test_session(key):
    value = session()
    value["labels"][key] = "true"
    assert plan(value).action == "preserve"


def test_changed_session_and_unproven_children_are_protected():
    assert plan(session(), unchanged_since_verification=False).action == "preserve"
    assert plan(session(has_children=True)).action == "preserve"
    assert plan(session(has_children=None)).action == "preserve"
    assert plan(session(parent_session_id="real-task")).action == "preserve"


def test_never_matches_title_age_or_test_like_content():
    value = session(labels={}, title="[TEST] temporary delete me", created_at=0)
    assert plan(value).action == "preserve"


def test_legacy_sessions_default_to_eligible_but_malformed_policy_fails_closed():
    assert session_score_eligible({})
    assert not session_score_eligible({SCORING_ELIGIBLE_LABEL: "False"})
    assert not session_score_eligible({TEST_RUN_LABEL: ""})


@pytest.mark.parametrize("args", [("", "codex"), ("run", " ")])
def test_creation_marker_validation(args):
    with pytest.raises(ValueError):
        make_test_labels(*args)


@pytest.mark.parametrize("reason", ["", " " * 10, "x" * 1001])
def test_keep_reason_validation(reason):
    with pytest.raises(ValueError):
        keep_for_inspection_labels(reason)
