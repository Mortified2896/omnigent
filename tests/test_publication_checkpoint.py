"""Publication finalizer regression tests."""

import pytest

from omnigent.publication_checkpoint import (
    PublicationCheckpointError,
    PublicationEvidence,
    PublicationState,
    finalize_publication,
)

SHA = "a" * 40


def test_false_completion_without_remote_evidence_is_rejected() -> None:
    with pytest.raises(PublicationCheckpointError, match="remote_commit"):
        finalize_publication(
            PublicationState.COMPLETED,
            PublicationEvidence(local_commit=SHA, worktree_clean=True),
        )


def test_remote_head_move_is_rejected() -> None:
    with pytest.raises(PublicationCheckpointError, match="do not match"):
        finalize_publication(
            PublicationState.COMPLETED,
            PublicationEvidence(
                local_commit=SHA,
                remote_commit="b" * 40,
                pr_head="b" * 40,
                pr_url="https://github.com/example/repo/pull/1",
                pr_base="main",
                worktree_clean=True,
            ),
        )


def test_push_failure_preserves_blocked_publication() -> None:
    assert (
        finalize_publication(
            PublicationState.BLOCKED_PUBLICATION,
            PublicationEvidence(local_commit=SHA, publication_error="git push: rejected"),
        )
        is PublicationState.BLOCKED_PUBLICATION
    )


def test_matching_readback_allows_completion() -> None:
    assert (
        finalize_publication(
            PublicationState.COMPLETED,
            PublicationEvidence(
                local_commit=SHA,
                remote_commit=SHA,
                pr_head=SHA,
                pr_url="https://github.com/example/repo/pull/1",
                pr_base="main",
                worktree_clean=True,
            ),
        )
        is PublicationState.COMPLETED
    )
