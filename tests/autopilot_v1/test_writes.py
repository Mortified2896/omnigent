"""Tests for the v1 write authority classifier."""

from __future__ import annotations

import pytest

from omnigent.autopilot_v1.writes import (
    WRITE_AUTHORITY,
    AuthorityDecision,
    GitHubWriteKind,
    GitWriteKind,
    Principal,
    WriteNotAllowedError,
    assert_write_allowed,
    classify_write,
)

# ── Allowed pairs ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind, principal",
    [
        (GitWriteKind.WORKER_COMMIT_LOCAL, Principal.WORKER),
        (GitWriteKind.WORKER_BRANCH_CREATE, Principal.WORKER),
        (GitWriteKind.ISSUE_BRANCH_PUSH, Principal.CONTROLLER),
        (GitWriteKind.PR_OPEN, Principal.CONTROLLER),
        (GitWriteKind.PR_REVIEW_REQUEST, Principal.CONTROLLER),
        (GitHubWriteKind.ISSUE_COMMENT, Principal.CONTROLLER),
        (GitHubWriteKind.PR_LABEL_ADD, Principal.CONTROLLER),
        (GitHubWriteKind.PROJECT_FIELD_UPDATE, Principal.CONTROLLER),
        (GitHubWriteKind.PR_CREATE, Principal.CONTROLLER),
        (GitHubWriteKind.ISSUE_STATUS_UPDATE, Principal.CONTROLLER),
        (GitWriteKind.PR_REVIEW_REQUEST, Principal.REVIEWER),
        (GitHubWriteKind.ISSUE_COMMENT, Principal.REVIEWER),
        (GitWriteKind.MERGE_TO_MAIN, Principal.HUMAN),
    ],
)
def test_allowed_pairs(kind: GitWriteKind | GitHubWriteKind, principal: Principal) -> None:
    assert classify_write(kind, principal) is AuthorityDecision.ALLOWED


# ── Worker prohibitions ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind",
    [
        GitWriteKind.ISSUE_BRANCH_PUSH,
        GitWriteKind.PR_OPEN,
        GitWriteKind.MERGE_TO_MAIN,
        GitHubWriteKind.ISSUE_STATUS_UPDATE,
        GitHubWriteKind.PR_CREATE,
        GitHubWriteKind.PROJECT_FIELD_UPDATE,
    ],
)
def test_worker_blocked_from_pushing_and_external_writes(
    kind: GitWriteKind | GitHubWriteKind,
) -> None:
    assert classify_write(kind, Principal.WORKER) is AuthorityDecision.BLOCKED_BY_POLICY


# ─- Controller prohibitions ───────────────────────────────────────────────


def test_controller_blocked_from_worker_local_writes() -> None:
    assert (
        classify_write(GitWriteKind.WORKER_COMMIT_LOCAL, Principal.CONTROLLER)
        is AuthorityDecision.BLOCKED_BY_POLICY
    )
    assert (
        classify_write(GitWriteKind.WORKER_BRANCH_CREATE, Principal.CONTROLLER)
        is AuthorityDecision.BLOCKED_BY_POLICY
    )


def test_controller_blocked_from_merge_to_main() -> None:
    assert (
        classify_write(GitWriteKind.MERGE_TO_MAIN, Principal.CONTROLLER)
        is AuthorityDecision.BLOCKED_BY_POLICY
    )


# ── Reviewer prohibitions ────────────────────────────────────────────────


def test_reviewer_blocked_from_push_and_pr_open() -> None:
    assert (
        classify_write(GitWriteKind.ISSUE_BRANCH_PUSH, Principal.REVIEWER)
        is AuthorityDecision.BLOCKED_BY_POLICY
    )
    assert (
        classify_write(GitWriteKind.PR_OPEN, Principal.REVIEWER)
        is AuthorityDecision.BLOCKED_BY_POLICY
    )


def test_reviewer_blocked_from_merge() -> None:
    assert (
        classify_write(GitWriteKind.MERGE_TO_MAIN, Principal.REVIEWER)
        is AuthorityDecision.BLOCKED_BY_POLICY
    )


# ── Merge-to-main is human-only ──────────────────────────────────────────


@pytest.mark.parametrize(
    "principal",
    [Principal.WORKER, Principal.CONTROLLER, Principal.REVIEWER],
)
def test_merge_to_main_blocked_for_non_human(
    principal: Principal,
) -> None:
    assert (
        classify_write(GitWriteKind.MERGE_TO_MAIN, principal)
        is AuthorityDecision.BLOCKED_BY_POLICY
    )


def test_merge_to_main_allowed_only_for_human() -> None:
    assert classify_write(GitWriteKind.MERGE_TO_MAIN, Principal.HUMAN) is AuthorityDecision.ALLOWED


# ── Unlisted pairs ───────────────────────────────────────────────────────


def test_unlisted_pair_defaults_to_blocked() -> None:
    # There is no entry for (PR_LABEL_ADD, REVIEWER) in the table; the
    # classifier must default to BLOCKED_BY_POLICY.
    assert (
        classify_write(GitHubWriteKind.PR_LABEL_ADD, Principal.REVIEWER)
        is AuthorityDecision.BLOCKED_BY_POLICY
    )


def test_write_authority_table_is_typed_correctly() -> None:
    # Belt-and-braces: ensure every entry's value is a real decision.
    for (_kind, _principal), decision in WRITE_AUTHORITY.items():
        assert isinstance(decision, AuthorityDecision)


# ── assert_write_allowed ─────────────────────────────────────────────────


def test_assert_write_allowed_passes_for_allowed_pair() -> None:
    # Should not raise.
    assert_write_allowed(GitWriteKind.WORKER_COMMIT_LOCAL, Principal.WORKER)


def test_assert_write_allowed_raises_for_blocked_pair() -> None:
    with pytest.raises(WriteNotAllowedError) as excinfo:
        assert_write_allowed(GitWriteKind.MERGE_TO_MAIN, Principal.CONTROLLER)
    err = excinfo.value
    assert err.kind is GitWriteKind.MERGE_TO_MAIN
    assert err.principal is Principal.CONTROLLER
    assert err.decision is AuthorityDecision.BLOCKED_BY_POLICY


def test_assert_write_allowed_error_message_diagnostic() -> None:
    with pytest.raises(WriteNotAllowedError) as excinfo:
        assert_write_allowed(GitWriteKind.ISSUE_BRANCH_PUSH, Principal.WORKER)
    msg = str(excinfo.value)
    assert "issue_branch_push" in msg
    assert "worker" in msg
    assert "blocked_by_policy" in msg
    assert "OVERSIGHT_AUTOPILOT_V1" in msg


def test_write_not_allowed_error_is_permission_error_subclass() -> None:
    with pytest.raises(PermissionError):
        assert_write_allowed(GitWriteKind.MERGE_TO_MAIN, Principal.WORKER)
