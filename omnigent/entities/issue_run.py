"""Issue-run entity for the Oversight Autopilot v1 contract.

Issue #18: durable issue-run persistence with atomic leasing. The
Autopilot needs one canonical, persistent record per (repository,
issue_number) so a restarted process can resume the exact run that
was in flight, while a competing process can never acquire the
same run concurrently.

A run is leased — an active ``lease_owner`` + ``lease_expires_at``
pair — so a crashed or timed-out runner does not strand a row
forever. Expired leases are recoverable without losing committed
work because every state transition is durable, idempotent, and
logged as an :class:`IssueRunEvent` row.

States form a one-way ladder (issue #17 / PR #27):
``queued -> claiming -> claimed -> in_progress -> pr_ready -> done``
with terminal failures ``failed`` and ``abandoned`` reachable from
any non-terminal state. ``claim_expired`` is a runtime-only flag
the store reads from ``lease_expires_at`` — never persisted as a
literal state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# Stable on-the-wire state literals; persisted verbatim in the
# ``state`` column. The contract is documented in
# ``omnigent.autopilot_v1.contracts``; this entity mirrors it so the
# store can enforce it without depending on the contracts module
# (which lives in a runtime path that the migration / store tests
# must not import).
class IssueRunState(str, Enum):
    """Lifecycle states for an Oversight Autopilot issue-run.

    The Autopilot moves through these in order; each transition is
    a durable INSERT into ``issue_runs`` (state field) plus an
    :class:`IssueRunEvent` row. The store refuses any transition that
    is not on the legal-edge list (see :func:`IssueRunStore.transition`).
    """

    QUEUED = "queued"
    CLAIMING = "claiming"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    PR_READY = "pr_ready"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass
class IssueRun:
    """One Autopilot run on one (repository, issue_number).

    The row is the source of truth for "is anyone working on issue
    N in repo R right now, and if so, who?" The store's atomic
    :meth:`IssueRunStore.try_claim` is the only writer of
    ``lease_owner`` + ``lease_expires_at``; the lease is the lock.

    :param id: Bare 32-char hex UUID primary key.
    :param repository: GitHub ``owner/repo`` slug.
    :param issue_number: The issue number the run targets.
    :param state: Current lifecycle state (see :class:`IssueRunState`).
    :param lease_owner: Bare 32-char hex UUID of the runner holding
        the lease, or ``None`` when no one is running this row.
    :param lease_acquired_at: Unix epoch seconds the lease was taken,
        or ``None``.
    :param lease_expires_at: Unix epoch seconds the lease lapses,
        or ``None``. While ``lease_expires_at`` is in the future the
        row is considered actively held; expired leases can be
        re-claimed by :meth:`IssueRunStore.reap_expired_leases`.
    :param parent_session_id: The parent harness session id that
        spawned the worker (a bare 32-char hex UUID).
    :param worker_session_id: The worker session id (a bare 32-char
        hex UUID), or ``None`` until the worker has started.
    :param branch: The branch the worker is using, e.g.
        ``"feat/issue-18-durable-run-persistence"``.
    :param worktree: Absolute path of the worker's worktree, or
        ``None`` until created.
    :param head_sha: Last commit sha the worker pushed, or ``None``
        until the worker has committed anything.
    :param pr_number: PR number this run opened, or ``None`` until
        opened.
    :param review_iteration: Current review / iteration counter
        (starts at 0; bumped per re-review).
    :param retry_count: Terminal retry counter (commits / pushes /
        migrations always retry-disabled).
    :param last_error: Most recent error message (truncated), or
        ``None`` when the run is healthy.
    :param last_error_code: Short classification (e.g. ``"timeout"``,
        ``"rate_limited"``), or ``None``.
    :param created_at: Unix epoch seconds the row was first written.
    :param updated_at: Unix epoch seconds the row was last touched.
    """

    id: str
    repository: str
    issue_number: int
    state: str
    workspace_id: int = 0
    lease_owner: str | None = None
    lease_acquired_at: int | None = None
    lease_expires_at: int | None = None
    parent_session_id: str | None = None
    worker_session_id: str | None = None
    branch: str | None = None
    worktree: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    review_iteration: int = 0
    retry_count: int = 0
    last_error: str | None = None
    last_error_code: str | None = None
    created_at: int | None = None
    updated_at: int | None = None


@dataclass
class IssueRunEvent:
    """One durable event in an :class:`IssueRun`'s lifecycle.

    The store writes one row per state transition + every external
    event the runner observed (PR opened, CI failed, comment posted).
    Replaying an event stream from ``created_at`` rebuilds the run
    without gaps — the source of truth for restart-safe checkpoint
    recovery.

    :param id: Bare 32-char hex UUID primary key.
    :param run_id: The owning :class:`IssueRun.id` (bare 32-char hex).
    :param sequence: Monotonic per-run sequence number (1, 2, 3, ...).
        The store assigns; callers should leave it ``None`` on insert.
    :param kind: Event kind — one of :data:`ISSUE_RUN_EVENT_KINDS`.
    :param from_state: Prior state (the ``state`` column was this when
        the event was emitted). ``None`` for creation events.
    :param to_state: New state after the event. ``None`` for pure
        observation events (e.g. ``"ci_failed"``).
    :param payload: Free-form JSON-serialisable payload (truncated
        log, CI url, comment id, etc.). Stored as JSON text.
    :param created_at: Unix epoch seconds the event row was written.
    """

    id: str
    run_id: str
    sequence: int
    kind: str
    from_state: str | None = None
    to_state: str | None = None
    payload: str | None = None
    created_at: int | None = None


# Stable event-kind literals. The store treats unknown kinds as
# ``None``-state observation events (recorded but not transitioning
# the run) so a future client can post new kinds without a migration.
ISSUE_RUN_EVENT_KINDS: tuple[str, ...] = (
    "created",
    "state_transition",
    "claim_acquired",
    "claim_extended",
    "claim_expired",
    "claim_recovered",
    "branch_created",
    "commit_pushed",
    "pr_opened",
    "pr_updated",
    "ci_passed",
    "ci_failed",
    "comment_posted",
    "review_received",
    "abandoned",
    "failed",
    "completed",
)


# Legal state edges. Forward-only; same-state (no-op) edges are
# rejected by the store to keep event-log semantics strict.
ISSUE_RUN_STATE_EDGES: dict[str, frozenset[str]] = {
    IssueRunState.QUEUED.value: frozenset({
        IssueRunState.CLAIMING.value,
        IssueRunState.ABANDONED.value,
    }),
    IssueRunState.CLAIMING.value: frozenset({
        IssueRunState.CLAIMED.value,
        IssueRunState.FAILED.value,
        IssueRunState.ABANDONED.value,
    }),
    IssueRunState.CLAIMED.value: frozenset({
        IssueRunState.IN_PROGRESS.value,
        IssueRunState.FAILED.value,
        IssueRunState.ABANDONED.value,
    }),
    IssueRunState.IN_PROGRESS.value: frozenset({
        IssueRunState.PR_READY.value,
        IssueRunState.FAILED.value,
        IssueRunState.ABANDONED.value,
    }),
    IssueRunState.PR_READY.value: frozenset({
        IssueRunState.DONE.value,
        IssueRunState.FAILED.value,
        IssueRunState.ABANDONED.value,
    }),
    IssueRunState.DONE.value: frozenset(),
    IssueRunState.FAILED.value: frozenset(),
    IssueRunState.ABANDONED.value: frozenset(),
}


def is_legal_state_edge(from_state: str, to_state: str) -> bool:
    """Return whether *from_state* -> *to_state* is on the legal-edge list.

    Used by the store's :meth:`transition` implementation and by tests
    that pin the contract. ``DONE`` / ``FAILED`` / ``ABANDONED`` are
    terminal: nothing leaves them.

    :param from_state: The current ``state`` value.
    :param to_state: The candidate next state.
    :returns: ``True`` when the transition is legal.
    """
    if from_state == to_state:
        # Same-state is rejected (callers should not emit a no-op
        # event; the store drops same-state transitions with a
        # dedicated error code so the caller can detect the typo).
        return False
    return to_state in ISSUE_RUN_STATE_EDGES.get(from_state, frozenset())


# Default lease duration for an Oversight Autopilot run. The store
# honours ``OMNIGENT_ISSUE_RUN_LEASE_S`` so a deployment can tune the
# horizon; ``None`` (zero / negative) means "no lease" and is not
# a supported mode for production deployments because expired-lease
# recovery is the only way a crashed runner hands control back.
DEFAULT_ISSUE_RUN_LEASE_S = 1800.0