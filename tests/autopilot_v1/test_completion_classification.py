"""Tests for the v1 completion classification helper."""

from __future__ import annotations

from omnigent.autopilot_v1 import (
    AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT,
    AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT,
    CompletionKind,
    IssueIdentifier,
    IssueRunStatus,
    OversightAutopilotState,
    classify_completion,
)


def _status(
    state: OversightAutopilotState,
    *,
    retry_count: int = 0,
    review_cycles_used: int = 0,
) -> IssueRunStatus:
    return IssueRunStatus(
        issue=IssueIdentifier(owner="octocat", repo="hello", number=1),
        state=state,
        retry_count=retry_count,
        review_cycles_used=review_cycles_used,
    )


# ── Happy path ───────────────────────────────────────────────────────────


def test_pr_ready_to_done_is_success_after_human_merge() -> None:
    # After a human merge the controller transitions PR_READY -> DONE,
    # so a status with state=DONE is the canonical "success".
    assert classify_completion(_status(OversightAutopilotState.DONE)) is (CompletionKind.SUCCESS)


def test_pr_ready_returns_pr_ready_awaiting_human_merge() -> None:
    assert (
        classify_completion(_status(OversightAutopilotState.PR_READY))
        is CompletionKind.PR_READY_AWAITING_HUMAN_MERGE
    )


# ── Blocked ──────────────────────────────────────────────────────────────


def test_blocked_returns_blocked_needs_human() -> None:
    assert (
        classify_completion(_status(OversightAutopilotState.BLOCKED))
        is CompletionKind.BLOCKED_NEEDS_HUMAN
    )


# ── Failed ───────────────────────────────────────────────────────────────


def test_failed_returns_failed_non_retryable() -> None:
    assert (
        classify_completion(_status(OversightAutopilotState.FAILED))
        is CompletionKind.FAILED_NON_RETRYABLE
    )


def test_failed_with_retry_exhaustion_returns_failed_retryable_exhausted() -> None:
    s = _status(
        OversightAutopilotState.FAILED,
        retry_count=AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT,
    )
    assert classify_completion(s) is CompletionKind.FAILED_RETRYABLE_EXHAUSTED


def test_failed_with_review_cycle_exhaustion_returns_failed_retryable_exhausted() -> None:
    s = _status(
        OversightAutopilotState.FAILED,
        review_cycles_used=AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT,
    )
    assert classify_completion(s) is CompletionKind.FAILED_RETRYABLE_EXHAUSTED


def test_failed_below_exhaustion_thresholds_is_non_retryable() -> None:
    # retry_count and review_cycles_used below their caps: the controller
    # routes the run to FAILED_NON_RETRYABLE (no budget left at this state).
    s = _status(
        OversightAutopilotState.FAILED,
        retry_count=AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT - 1,
    )
    assert classify_completion(s) is CompletionKind.FAILED_NON_RETRYABLE


# ─- In progress ──────────────────────────────────────────────────────────


def test_in_progress_states_return_in_progress() -> None:
    for state in (
        OversightAutopilotState.QUEUED,
        OversightAutopilotState.CLAIMED,
        OversightAutopilotState.PLANNING,
        OversightAutopilotState.IMPLEMENTING,
        OversightAutopilotState.TESTING,
        OversightAutopilotState.REVIEWING,
        OversightAutopilotState.FIXING,
        OversightAutopilotState.PUBLISHING,
    ):
        assert classify_completion(_status(state)) is CompletionKind.IN_PROGRESS, state
