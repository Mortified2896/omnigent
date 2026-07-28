"""Oversight Autopilot v1 — write authority classifier.

Pure classifier: given a (kind, principal) pair, decide whether the
write is allowed, requires human approval, or is blocked by policy. No
actual writes happen here. The controller (issue #22) is expected to
call :func:`assert_write_allowed` before issuing any GitHub or Git
operation on behalf of the autopilot.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Union

WriteKind = Union["GitWriteKind", "GitHubWriteKind"]


class GitWriteKind(StrEnum):
    """A local Git write operation that the autopilot might request."""

    ISSUE_BRANCH_PUSH = "issue_branch_push"
    PR_OPEN = "pr_open"
    PR_REVIEW_REQUEST = "pr_review_request"
    MERGE_TO_MAIN = "merge_to_main"
    WORKER_COMMIT_LOCAL = "worker_commit_local"
    WORKER_BRANCH_CREATE = "worker_branch_create"


class GitHubWriteKind(StrEnum):
    """A GitHub API write operation that the autopilot might request."""

    ISSUE_STATUS_UPDATE = "issue_status_update"
    ISSUE_COMMENT = "issue_comment"
    PR_CREATE = "pr_create"
    PR_LABEL_ADD = "pr_label_add"
    PROJECT_FIELD_UPDATE = "project_field_update"


class Principal(StrEnum):
    """Who is asking to perform the write.

    ``WORKER`` is the implementation agent (sandboxed worktree).
    ``CONTROLLER`` is the orchestration layer that runs outside the
    sandbox. ``REVIEWER`` is the independent review agent.
    ``HUMAN`` is a person acting through the web UI / CLI.
    """

    WORKER = "worker"
    CONTROLLER = "controller"
    REVIEWER = "reviewer"
    HUMAN = "human"


class AuthorityDecision(StrEnum):
    """The outcome of a write-authority classification."""

    ALLOWED = "allowed"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    REQUIRES_HUMAN_APPROVAL = "requires_human_approval"


# ── Authority table ──────────────────────────────────────────────────────
#
# The table is intentionally exhaustive for the v1 primitives. Every
# (kind, principal) pair is either explicitly listed or — by the
# ``classify_write`` fallback — defaults to BLOCKED_BY_POLICY. The
# whitelist approach keeps surprises off the table: an operator must
# consciously enable a write, not accidentally inherit it.

WRITE_AUTHORITY: dict[tuple[WriteKind, Principal], AuthorityDecision] = {
    # Worker local Git writes (sandboxed worktree).
    (GitWriteKind.WORKER_COMMIT_LOCAL, Principal.WORKER): AuthorityDecision.ALLOWED,
    (GitWriteKind.WORKER_BRANCH_CREATE, Principal.WORKER): AuthorityDecision.ALLOWED,
    # Worker MUST NOT push branches or open PRs — those are controller
    # responsibilities. Worker MUST NOT update issue status, create PRs,
    # or update project fields.
    (GitWriteKind.ISSUE_BRANCH_PUSH, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.PR_OPEN, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.PR_REVIEW_REQUEST, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.MERGE_TO_MAIN, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitHubWriteKind.ISSUE_STATUS_UPDATE, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitHubWriteKind.ISSUE_COMMENT, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitHubWriteKind.PR_CREATE, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitHubWriteKind.PR_LABEL_ADD, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitHubWriteKind.PROJECT_FIELD_UPDATE, Principal.WORKER): AuthorityDecision.BLOCKED_BY_POLICY,
    # Controller responsibilities: push the issue branch, open the PR,
    # request the review, comment, label, update project fields.
    (GitWriteKind.ISSUE_BRANCH_PUSH, Principal.CONTROLLER): AuthorityDecision.ALLOWED,
    (GitWriteKind.PR_OPEN, Principal.CONTROLLER): AuthorityDecision.ALLOWED,
    (GitWriteKind.PR_REVIEW_REQUEST, Principal.CONTROLLER): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.ISSUE_COMMENT, Principal.CONTROLLER): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.PR_LABEL_ADD, Principal.CONTROLLER): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.PROJECT_FIELD_UPDATE, Principal.CONTROLLER): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.PR_CREATE, Principal.CONTROLLER): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.ISSUE_STATUS_UPDATE, Principal.CONTROLLER): AuthorityDecision.ALLOWED,
    # Controller MUST NOT touch worker-local writes or merge to main.
    (GitWriteKind.WORKER_COMMIT_LOCAL, Principal.CONTROLLER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.WORKER_BRANCH_CREATE, Principal.CONTROLLER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.MERGE_TO_MAIN, Principal.CONTROLLER): AuthorityDecision.BLOCKED_BY_POLICY,
    # Reviewer responsibilities: comment and request reviews.
    (GitWriteKind.PR_REVIEW_REQUEST, Principal.REVIEWER): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.ISSUE_COMMENT, Principal.REVIEWER): AuthorityDecision.ALLOWED,
    # Reviewer MUST NOT push, open PRs, or touch project state.
    (GitWriteKind.ISSUE_BRANCH_PUSH, Principal.REVIEWER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.PR_OPEN, Principal.REVIEWER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.WORKER_COMMIT_LOCAL, Principal.REVIEWER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.WORKER_BRANCH_CREATE, Principal.REVIEWER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitWriteKind.MERGE_TO_MAIN, Principal.REVIEWER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitHubWriteKind.ISSUE_STATUS_UPDATE, Principal.REVIEWER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitHubWriteKind.PR_CREATE, Principal.REVIEWER): AuthorityDecision.BLOCKED_BY_POLICY,
    (GitHubWriteKind.PR_LABEL_ADD, Principal.REVIEWER): AuthorityDecision.BLOCKED_BY_POLICY,
    (
        GitHubWriteKind.PROJECT_FIELD_UPDATE,
        Principal.REVIEWER,
    ): AuthorityDecision.BLOCKED_BY_POLICY,
    # Human may do anything. MERGE_TO_MAIN is the canonical example —
    # only a human can merge to the base branch.
    (GitWriteKind.MERGE_TO_MAIN, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitWriteKind.ISSUE_BRANCH_PUSH, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitWriteKind.PR_OPEN, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitWriteKind.PR_REVIEW_REQUEST, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitWriteKind.WORKER_COMMIT_LOCAL, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitWriteKind.WORKER_BRANCH_CREATE, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.ISSUE_STATUS_UPDATE, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.ISSUE_COMMENT, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.PR_CREATE, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.PR_LABEL_ADD, Principal.HUMAN): AuthorityDecision.ALLOWED,
    (GitHubWriteKind.PROJECT_FIELD_UPDATE, Principal.HUMAN): AuthorityDecision.ALLOWED,
}


# ── Classifier + helpers ─────────────────────────────────────────────────


class WriteNotAllowedError(PermissionError):
    """Raised by :func:`assert_write_allowed` for a denied write."""

    def __init__(
        self,
        kind: WriteKind,
        principal: Principal,
        decision: AuthorityDecision,
        detail: str = "",
    ) -> None:
        self.kind = kind
        self.principal = principal
        self.decision = decision
        self.detail = detail
        message = (
            f"Oversight Autopilot v1 write denied: kind={kind.value!r} "
            f"principal={principal.value!r} decision={decision.value!r}."
        )
        if detail:
            message = f"{message} {detail}"
        message = (
            f"{message} Consult docs/OVERSIGHT_AUTOPILOT_V1.md "
            "(Authority boundaries) before retrying."
        )
        super().__init__(message)

    def __str__(self) -> str:
        return self.args[0] if self.args else super().__str__()


def classify_write(kind: WriteKind, principal: Principal) -> AuthorityDecision:
    """Return the authority decision for a (kind, principal) pair.

    Unlisted pairs default to :attr:`AuthorityDecision.BLOCKED_BY_POLICY`
    so the table is treated as a whitelist. This is deliberate: the
    v1 contract prefers a loud denial over a silent acceptance.
    """
    return WRITE_AUTHORITY.get((kind, principal), AuthorityDecision.BLOCKED_BY_POLICY)


def assert_write_allowed(kind: WriteKind, principal: Principal) -> None:
    """Raise :class:`WriteNotAllowedError` if the write is not allowed."""
    decision = classify_write(kind, principal)
    if decision is AuthorityDecision.ALLOWED:
        return
    detail = ""
    if decision is AuthorityDecision.REQUIRES_HUMAN_APPROVAL:
        detail = "A human must approve this write before it can proceed."
    elif decision is AuthorityDecision.BLOCKED_BY_POLICY:
        detail = (
            "The v1 contract forbids this principal from performing this write; "
            "see the authority table in docs/OVERSIGHT_AUTOPILOT_V1.md."
        )
    raise WriteNotAllowedError(
        kind=kind,
        principal=principal,
        decision=decision,
        detail=detail,
    )
