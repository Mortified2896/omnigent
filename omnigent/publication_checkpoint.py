"""Fail-closed publication evidence for durable task finalization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PublicationState(StrEnum):
    CHECKPOINTED = "checkpointed"
    COMPLETED = "completed"
    BLOCKED_PUBLICATION = "blocked_publication"


class PublicationCheckpointError(ValueError):
    """Raised when a task claims a terminal state without durable evidence."""


@dataclass(frozen=True)
class PublicationEvidence:
    local_commit: str | None = None
    remote_commit: str | None = None
    pr_url: str | None = None
    pr_head: str | None = None
    pr_base: str | None = None
    worktree_clean: bool = False
    publication_error: str | None = None


def finalize_publication(
    claimed_state: PublicationState,
    evidence: PublicationEvidence,
) -> PublicationState:
    """Validate read-back evidence instead of trusting a completion report."""
    if claimed_state is PublicationState.BLOCKED_PUBLICATION:
        if evidence.local_commit is None or not evidence.publication_error:
            raise PublicationCheckpointError(
                "blocked_publication requires a local commit and exact publication error"
            )
        return claimed_state

    required = {
        "local_commit": evidence.local_commit,
        "remote_commit": evidence.remote_commit,
        "pr_url": evidence.pr_url,
        "pr_head": evidence.pr_head,
        "pr_base": evidence.pr_base,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise PublicationCheckpointError(f"publication evidence missing: {', '.join(missing)}")
    if (
        evidence.local_commit != evidence.remote_commit
        or evidence.pr_head != evidence.remote_commit
    ):
        raise PublicationCheckpointError("local, remote, and PR heads do not match")
    if claimed_state is PublicationState.COMPLETED and not evidence.worktree_clean:
        raise PublicationCheckpointError("completed requires a clean task worktree")
    return claimed_state
