"""Tests for the v1 contract dataclasses."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.autopilot_v1 import (
    AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT,
    AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT,
    ClarificationRequest,
    Eligibility,
    IssueIdentifier,
    IssueRunStatus,
    OversightAutopilotState,
    is_eligible_for_run,
    requires_clarification,
)

# ── IssueIdentifier ──────────────────────────────────────────────────────


def test_issue_identifier_accepts_valid_names() -> None:
    ident = IssueIdentifier(owner="octocat", repo="hello-world", number=42)
    assert ident.owner == "octocat"
    assert ident.repo == "hello-world"
    assert ident.number == 42


def test_issue_identifier_rejects_empty_owner() -> None:
    with pytest.raises(ValidationError) as excinfo:
        IssueIdentifier(owner="", repo="hello", number=1)
    assert "owner" in str(excinfo.value)


def test_issue_identifier_rejects_invalid_owner_chars() -> None:
    with pytest.raises(ValidationError) as excinfo:
        IssueIdentifier(owner="octo cat", repo="hello", number=1)
    assert "owner" in str(excinfo.value)


def test_issue_identifier_rejects_leading_dash_owner() -> None:
    with pytest.raises(ValidationError):
        IssueIdentifier(owner="-octocat", repo="hello", number=1)


def test_issue_identifier_rejects_leading_dot_owner() -> None:
    with pytest.raises(ValidationError):
        IssueIdentifier(owner=".octocat", repo="hello", number=1)


def test_issue_identifier_rejects_empty_repo() -> None:
    with pytest.raises(ValidationError) as excinfo:
        IssueIdentifier(owner="octocat", repo="", number=1)
    assert "repo" in str(excinfo.value)


def test_issue_identifier_rejects_invalid_repo_chars() -> None:
    with pytest.raises(ValidationError) as excinfo:
        IssueIdentifier(owner="octocat", repo="hello/world", number=1)
    assert "repo" in str(excinfo.value)


def test_issue_identifier_rejects_zero_number() -> None:
    with pytest.raises(ValidationError) as excinfo:
        IssueIdentifier(owner="octocat", repo="hello", number=0)
    assert "number" in str(excinfo.value)


def test_issue_identifier_rejects_negative_number() -> None:
    with pytest.raises(ValidationError):
        IssueIdentifier(owner="octocat", repo="hello", number=-1)


def test_issue_identifier_is_frozen() -> None:
    ident = IssueIdentifier(owner="octocat", repo="hello", number=1)
    with pytest.raises(ValidationError):
        ident.number = 99  # type: ignore[misc]


# ── Eligibility ──────────────────────────────────────────────────────────


def test_eligibility_default_is_not_eligible() -> None:
    e = Eligibility()
    assert e.is_eligible is False
    assert is_eligible_for_run(e) is False


def test_eligibility_is_eligible_for_run_only_when_all_gates_true() -> None:
    e = Eligibility(
        is_eligible=True,
        eligible_repository=True,
        within_active_issue_limit=True,
        has_required_labels=True,
    )
    assert is_eligible_for_run(e) is True


def test_eligibility_with_clarifications_not_eligible() -> None:
    e = Eligibility(
        is_eligible=True,
        eligible_repository=True,
        within_active_issue_limit=True,
        has_required_labels=True,
        clarifications_required=["Which CLI subcommand?"],
    )
    assert is_eligible_for_run(e) is False


def test_eligibility_with_disqualification_not_eligible() -> None:
    e = Eligibility(
        is_eligible=False,
        eligible_repository=True,
        within_active_issue_limit=True,
        has_required_labels=True,
        disqualifications=["repo not allowlisted"],
    )
    assert is_eligible_for_run(e) is False


def test_eligibility_must_have_each_gate() -> None:
    base = {
        "is_eligible": True,
        "eligible_repository": True,
        "within_active_issue_limit": True,
        "has_required_labels": True,
    }
    for missing in (
        "eligible_repository",
        "within_active_issue_limit",
        "has_required_labels",
        "is_eligible",
    ):
        e = Eligibility(**{**base, missing: False})
        assert is_eligible_for_run(e) is False, missing


# ── requires_clarification helper ────────────────────────────────────────


def test_requires_clarification_true_when_non_empty() -> None:
    assert requires_clarification(["q1"]) is True


def test_requires_clarification_false_when_empty() -> None:
    assert requires_clarification([]) is False


# ── ClarificationRequest ─────────────────────────────────────────────────


def test_clarification_request_default_blocking() -> None:
    req = ClarificationRequest(
        issue=IssueIdentifier(owner="octocat", repo="hello", number=1),
        questions=["Which command?"],
    )
    assert req.blocking is True


def test_clarification_request_rejects_empty_questions() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ClarificationRequest(
            issue=IssueIdentifier(owner="octocat", repo="hello", number=1),
            questions=[],
        )
    assert "questions" in str(excinfo.value)


def test_clarification_request_non_blocking() -> None:
    req = ClarificationRequest(
        issue=IssueIdentifier(owner="octocat", repo="hello", number=1),
        questions=["FYI"],
        blocking=False,
    )
    assert req.blocking is False


# ── IssueRunStatus ───────────────────────────────────────────────────────


def _issue() -> IssueIdentifier:
    return IssueIdentifier(owner="octocat", repo="hello", number=1)


def test_issue_run_status_defaults() -> None:
    s = IssueRunStatus(issue=_issue(), state=OversightAutopilotState.QUEUED)
    assert s.retry_count == 0
    assert s.review_cycles_used == 0
    assert s.last_transition_at is None
    assert s.last_transition_reason is None


def test_issue_run_status_terminal_flag_derived_for_done() -> None:
    s = IssueRunStatus(issue=_issue(), state=OversightAutopilotState.DONE)
    assert s.terminal is True


def test_issue_run_status_terminal_flag_derived_for_failed() -> None:
    s = IssueRunStatus(issue=_issue(), state=OversightAutopilotState.FAILED)
    assert s.terminal is True


def test_issue_run_status_human_intervention_flag_for_pr_ready() -> None:
    s = IssueRunStatus(issue=_issue(), state=OversightAutopilotState.PR_READY)
    assert s.requires_human_intervention is True


def test_issue_run_status_human_intervention_flag_for_blocked() -> None:
    s = IssueRunStatus(issue=_issue(), state=OversightAutopilotState.BLOCKED)
    assert s.requires_human_intervention is True


def test_issue_run_status_rejects_retry_count_above_cap() -> None:
    over_cap = AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT + 1
    with pytest.raises(ValidationError) as excinfo:
        IssueRunStatus(
            issue=_issue(),
            state=OversightAutopilotState.IMPLEMENTING,
            retry_count=over_cap,
        )
    assert "retry_count" in str(excinfo.value)


def test_issue_run_status_rejects_review_cycles_above_cap() -> None:
    over_cap = AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT + 1
    with pytest.raises(ValidationError) as excinfo:
        IssueRunStatus(
            issue=_issue(),
            state=OversightAutopilotState.REVIEWING,
            review_cycles_used=over_cap,
        )
    assert "review_cycles_used" in str(excinfo.value)


def test_issue_run_status_accepts_retry_count_at_cap() -> None:
    s = IssueRunStatus(
        issue=_issue(),
        state=OversightAutopilotState.IMPLEMENTING,
        retry_count=AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT,
    )
    assert s.retry_count == AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT


def test_issue_run_status_accepts_review_cycles_at_cap() -> None:
    s = IssueRunStatus(
        issue=_issue(),
        state=OversightAutopilotState.REVIEWING,
        review_cycles_used=AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT,
    )
    assert s.review_cycles_used == AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT


def test_issue_run_status_rejects_negative_retry_count() -> None:
    with pytest.raises(ValidationError):
        IssueRunStatus(
            issue=_issue(),
            state=OversightAutopilotState.IMPLEMENTING,
            retry_count=-1,
        )


def test_issue_run_status_rejects_negative_review_cycles() -> None:
    with pytest.raises(ValidationError):
        IssueRunStatus(
            issue=_issue(),
            state=OversightAutopilotState.REVIEWING,
            review_cycles_used=-1,
        )


def test_issue_run_status_is_frozen() -> None:
    s = IssueRunStatus(issue=_issue(), state=OversightAutopilotState.QUEUED)
    with pytest.raises(ValidationError):
        s.state = OversightAutopilotState.DONE  # type: ignore[misc]
