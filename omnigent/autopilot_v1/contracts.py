"""Oversight Autopilot v1 — cross-layer contracts (frozen pydantic models).

These shapes are the contract surface between the controller (issue #22),
the persistence layer (issue #18), the publication controller (issue #24),
and the workers (issue #21). They are pure in-memory data — there is no
DB I/O, no scheduling, no GitHub polling. Persistence will be wired up
in #18 and beyond.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from omnigent.autopilot_v1.states import (
    COMPLETION_STATES,
    OversightAutopilotState,
    is_terminal,
    requires_human_intervention,
)

# ── Limits constants (mirror config defaults) ────────────────────────────

AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT: int = 3
AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT: int = 2


# ── GitHub identifier pattern (shared with config) ───────────────────────

_GITHUB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_GITHUB_NAME_LEADING = re.compile(r"^[.-]")


def _check_github_name(value: str, *, field: str) -> str:
    """Validate a GitHub owner/repo name. Returns the value unchanged."""
    if not value:
        raise ValueError(f"{field} must be non-empty")
    if _GITHUB_NAME_LEADING.match(value):
        raise ValueError(f"{field}={value!r} must not start with '.' or '-'; GitHub rejects these")
    if not _GITHUB_NAME_PATTERN.match(value):
        raise ValueError(f"{field}={value!r} contains characters outside [A-Za-z0-9._-]")
    return value


# ── IssueIdentifier ──────────────────────────────────────────────────────


class IssueIdentifier(BaseModel):
    """A GitHub (owner, repo, number) triple identifying one issue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str
    repo: str
    number: int

    @model_validator(mode="after")
    def _validate(self) -> IssueIdentifier:
        owner = _check_github_name(self.owner, field="owner")
        repo = _check_github_name(self.repo, field="repo")
        if self.number < 1:
            raise ValueError(
                f"number={self.number} must be >= 1 (GitHub issue numbers are positive)"
            )
        if owner != self.owner or repo != self.repo:
            object.__setattr__(self, "owner", owner)
            object.__setattr__(self, "repo", repo)
        return self


# ── Eligibility ──────────────────────────────────────────────────────────


class Eligibility(BaseModel):
    """Whether an issue is eligible for v1 Autopilot.

    Each gate is exposed as a separate boolean so a controller can
    surface partial progress (e.g. ``eligible_repository=True`` but
    ``has_required_labels=False``). ``is_eligible`` is the aggregate;
    use :func:`is_eligible_for_run` to consult it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    is_eligible: bool = False
    eligible_repository: bool = False
    within_active_issue_limit: bool = False
    has_required_labels: bool = False
    clarifications_required: list[str] = []
    disqualifications: list[str] = []


def is_eligible_for_run(eligibility: Eligibility) -> bool:
    """Return ``True`` iff the issue is eligible and free of required clarifications."""
    if not eligibility.is_eligible:
        return False
    if not eligibility.eligible_repository:
        return False
    if not eligibility.within_active_issue_limit:
        return False
    if not eligibility.has_required_labels:
        return False
    if eligibility.clarifications_required:
        return False
    return True


def requires_clarification(clarifications: list[str]) -> bool:
    """Return ``True`` iff there is at least one outstanding clarification."""
    return len(clarifications) > 0


# ── Clarification ────────────────────────────────────────────────────────


class ClarificationRequest(BaseModel):
    """A human clarification posted on the issue.

    ``blocking=True`` (the default) means the controller will transition
    the run to ``BLOCKED`` until the human answers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue: IssueIdentifier
    questions: list[str]
    blocking: bool = True

    @model_validator(mode="after")
    def _validate(self) -> ClarificationRequest:
        if not self.questions:
            raise ValueError(
                "questions must be non-empty; an empty ClarificationRequest "
                "is not a valid clarification"
            )
        return self


# ── IssueRunStatus ───────────────────────────────────────────────────────


class IssueRunStatus(BaseModel):
    """A snapshot of an in-progress issue run.

    The controller produces these snapshots as it transitions a run; the
    persistence layer (issue #18) will store them. ``terminal`` and
    ``requires_human_intervention`` are derived from ``state`` so a
    consumer never has to recompute them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue: IssueIdentifier
    state: OversightAutopilotState
    retry_count: int = 0
    review_cycles_used: int = 0
    last_transition_at: int | None = None
    last_transition_reason: str | None = None
    terminal: bool = False
    requires_human_intervention: bool = False

    @model_validator(mode="after")
    def _validate(self) -> IssueRunStatus:
        if self.retry_count < 0:
            raise ValueError(f"retry_count={self.retry_count} must be >= 0")
        if self.retry_count > AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT:
            raise ValueError(
                f"retry_count={self.retry_count} exceeds the v1 default cap "
                f"({AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT}); the controller "
                "should mark this run FAILED_RETRYABLE_EXHAUSTED."
            )
        if self.review_cycles_used < 0:
            raise ValueError(f"review_cycles_used={self.review_cycles_used} must be >= 0")
        if self.review_cycles_used > AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT:
            raise ValueError(
                f"review_cycles_used={self.review_cycles_used} exceeds the v1 "
                f"default cap ({AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT}); "
                "the controller should stop routing review cycles."
            )
        derived_terminal = is_terminal(self.state)
        derived_human = requires_human_intervention(self.state)
        if self.terminal != derived_terminal:
            object.__setattr__(self, "terminal", derived_terminal)
        if self.requires_human_intervention != derived_human:
            object.__setattr__(self, "requires_human_intervention", derived_human)
        return self


# ── Completion classification ────────────────────────────────────────────


class CompletionKind(StrEnum):
    """One of the discrete outcomes the controller can report on a run.

    These are the values the future controller emits into the Langfuse
    / outbox layer (issue #23) when a run reaches a completion state.
    """

    SUCCESS = "success"
    BLOCKED_NEEDS_HUMAN = "blocked_needs_human"
    PR_READY_AWAITING_HUMAN_MERGE = "pr_ready_awaiting_human_merge"
    FAILED_NON_RETRYABLE = "failed_non_retryable"
    FAILED_RETRYABLE_EXHAUSTED = "failed_retryable_exhausted"
    IN_PROGRESS = "in_progress"


def classify_completion(status: IssueRunStatus) -> CompletionKind:
    """Classify a run snapshot into a :class:`CompletionKind`.

    The classification is intentionally state-centric with retry/review
    exhaustion layered on top of ``FAILED`` and ``PR_READY`` respectively.
    """
    state = status.state
    if state in COMPLETION_STATES:
        if state is OversightAutopilotState.DONE:
            return CompletionKind.SUCCESS
        if state is OversightAutopilotState.PR_READY:
            return CompletionKind.PR_READY_AWAITING_HUMAN_MERGE
    if state is OversightAutopilotState.BLOCKED:
        return CompletionKind.BLOCKED_NEEDS_HUMAN
    if state is OversightAutopilotState.FAILED:
        # Both retry-budget and review-cycle exhaustion collapse to
        # ``FAILED`` at the state-machine level; surface the more
        # specific classification via the counter that exceeded its cap.
        if status.review_cycles_used >= AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT:
            return CompletionKind.FAILED_RETRYABLE_EXHAUSTED
        if status.retry_count >= AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT:
            return CompletionKind.FAILED_RETRYABLE_EXHAUSTED
        return CompletionKind.FAILED_NON_RETRYABLE
    return CompletionKind.IN_PROGRESS
