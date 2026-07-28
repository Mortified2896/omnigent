"""Issue-run store — durable, atomic, leased persistence for the Autopilot.

Issue #18: one canonical :class:`IssueRun` per (repository, issue)
so a restarted process resumes exactly the run that was in flight,
while two competing processes can never both hold the same run.
The store is the only writer of the ``lease_owner`` + ``lease_expires_at``
pair and therefore the only thing that turns ``atomic claim`` from
a wish into a database invariant.

The contract:

- :meth:`try_claim` is the single entry point for acquiring an
  issue-run. It runs an atomic INSERT (when no row exists) or an
  atomic UPDATE-IF-EXPIRED (when a stale lease exists) inside one
  transaction so two callers cannot both win. The lease TTL is the
  configurable :data:`DEFAULT_ISSUE_RUN_LEASE_S` ceiling.
- :meth:`extend_lease` refreshes the lease for a row the caller
  already holds (used during long turns).
- :meth:`release_lease` is the explicit hand-off the runner uses
  on graceful shutdown so a sibling can immediately re-claim rather
  than waiting out the TTL.
- :meth:`transition` advances ``state`` along the legal-edge graph
  in :data:`omnigent.entities.issue_run.ISSUE_RUN_STATE_EDGES`.
  Every transition writes an :class:`IssueRunEvent` so the audit
  trail survives a crash.
- :meth:`reap_expired_leases` finds rows whose ``lease_expires_at``
  is in the past, marks them :data:`IssueRunState.ABANDONED`, and
  emits ``claim_expired`` / ``claim_recovered`` events so the next
  caller can re-claim without losing committed work.
- :meth:`list_active` returns every row in a non-terminal state;
  the global V1 invariant (``at most one active run globally``)
  is a derived check the caller performs on the result.

The store is process-wide (one SQLite session / PG pool per
process). Cross-process safety comes from the SQL transaction
itself: every method that mutates lease / state runs in a single
``engine.begin()`` block so the database is the linearisation
point.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities.issue_run import (
    DEFAULT_ISSUE_RUN_LEASE_S,
    ISSUE_RUN_EVENT_KINDS,
    ISSUE_RUN_STATE_EDGES,
    IssueRun,
    IssueRunEvent,
    IssueRunState,
    is_legal_state_edge,
)


class IssueRunConflictError(RuntimeError):
    """Raised when a lease / claim attempt collides with a live lease.

    Callers catch this to fall back to ``release_lease`` +
    ``try_claim`` of a sibling lease, or to surface the conflict to
    the user.
    """

    def __init__(self, repository: str, issue_number: int, message: str) -> None:
        super().__init__(message)
        self.repository = repository
        self.issue_number = issue_number


class IssueRunStateError(RuntimeError):
    """Raised when a state transition is not on the legal-edge graph.

    Surfaces a ``(from_state, to_state, run_id)`` triple so the
    caller can pinpoint the offending row.
    """

    def __init__(self, run_id: str, from_state: str, to_state: str) -> None:
        super().__init__(
            f"illegal state transition for run {run_id!r}: {from_state!r} -> {to_state!r}"
        )
        self.run_id = run_id
        self.from_state = from_state
        self.to_state = to_state


def _new_run_id() -> str:
    """Return a fresh bare 32-char hex uuid."""
    return uuid.uuid4().hex


def _now_epoch() -> int:
    """Return the current unix-epoch seconds (int)."""
    return int(time.time())


def _lease_ttl_s() -> float:
    """Resolve the lease TTL from ``OMNIGENT_ISSUE_RUN_LEASE_S``."""
    raw = os.environ.get("OMNIGENT_ISSUE_RUN_LEASE_S")
    if raw is None:
        return DEFAULT_ISSUE_RUN_LEASE_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ISSUE_RUN_LEASE_S
    return value if value > 0 else DEFAULT_ISSUE_RUN_LEASE_S


class IssueRunStore(ABC):
    """Abstract base for issue-run persistence.

    Mirrors the canonical pattern used by :class:`ScheduledTaskStore`
    and the other Omnigent stores: an abstract ``storage_location``
    ctor plus a small set of atomic CRUD methods. The SQLAlchemy
    implementation lives in
    :mod:`omnigent.stores.issue_run_store.sqlalchemy_store`; tests
    substitute their own implementation when needed.
    """

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location

    # ── Reads ────────────────────────────────────────────────

    @abstractmethod
    def get_by_run_id(self, run_id: str) -> IssueRun | None:
        """Return the run with id *run_id*, or ``None`` if missing."""

    @abstractmethod
    def get_active(self, repository: str, issue_number: int) -> IssueRun | None:
        """Return the non-terminal run for *(repository, issue_number)*.

        ``None`` when no row exists or the only row is in a
        terminal state — the canonical "no current run" answer the
        ``try_claim`` call uses to decide whether to insert vs.
        re-claim.
        """

    @abstractmethod
    def list_active(self) -> list[IssueRun]:
        """Return every row in a non-terminal state.

        Used by the V1 invariant check (``at most one active run
        globally``) and by the recovery sweep that runs on
        deployment startup.
        """

    @abstractmethod
    def list_events(self, run_id: str) -> list[IssueRunEvent]:
        """Return every :class:`IssueRunEvent` row for *run_id* in
        ``created_at`` ascending order. The store assigns the
        ``sequence`` column; callers receive 1-indexed, gap-free
        sequence numbers for the run.
        """

    # ── Writes ───────────────────────────────────────────────

    @abstractmethod
    def try_claim(
        self,
        repository: str,
        issue_number: int,
        *,
        lease_owner: str | None = None,
        parent_session_id: str | None = None,
        lease_ttl_s: float | None = None,
    ) -> IssueRun:
        """Atomically claim the run for *(repository, issue_number)*.

        The transaction is the linearisation point:

        - No existing row: insert one in :data:`IssueRunState.QUEUED`
          state with ``lease_owner`` set + ``lease_expires_at`` set
          to ``now + lease_ttl_s``.
        - Existing row in a non-terminal state with a live lease:
          raise :class:`IssueRunConflictError` (caller must wait or
          release the active lease).
        - Existing row in a non-terminal state with an *expired*
          lease: take the lease (UPDATE the lease_owner /
          lease_expires_at pair) and stay in whatever state the run
          is currently in. Emits ``claim_recovered`` so the audit
          log records the recovery.
        - Existing row in a terminal state (``DONE`` / ``FAILED`` /
          ``ABANDONED``): raise :class:`IssueRunConflictError`. The
          autopilot contract requires a fresh run id for a
          re-attempt; ``create_follow_up`` is the helper that
          inserts the successor row.

        :param repository: ``owner/repo`` slug.
        :param issue_number: Issue number to claim.
        :param lease_owner: Caller identity (bare 32-char hex UUID
            or any stable opaque token). When ``None`` the store
            generates one.
        :param parent_session_id: Optional parent harness session
            id recorded on the row.
        :param lease_ttl_s: Override for the default lease TTL;
            ``None`` uses :func:`_lease_ttl_s`.
        :returns: The persisted :class:`IssueRun` after the claim
            has been recorded.
        :raises IssueRunConflictError: When a live lease already
            holds the row, or the row is in a terminal state.
        """

    @abstractmethod
    def extend_lease(
        self,
        run_id: str,
        *,
        lease_owner: str,
        lease_ttl_s: float | None = None,
    ) -> IssueRun:
        """Refresh *run_id*'s lease to ``now + ttl`` iff *lease_owner* still holds it.

        Idempotent on a stale lease: the call returns the current
        row unchanged when the lease has already expired.

        :param run_id: The run to refresh.
        :param lease_owner: The current holder; an ``IssueRunConflictError``
            is raised if a different caller now holds the lease.
        :param lease_ttl_s: Override for the lease TTL.
        :returns: The persisted :class:`IssueRun` with the new
            ``lease_expires_at``.
        :raises IssueRunConflictError: When a different lease_owner
            holds the lease, or the run is terminal.
        """

    @abstractmethod
    def release_lease(self, run_id: str, *, lease_owner: str) -> None:
        """Release the lease on *run_id* iff *lease_owner* holds it.

        Used on graceful runner shutdown. The run stays in its
        current state; only ``lease_owner`` / ``lease_acquired_at`` /
        ``lease_expires_at`` are cleared. Emits a ``claim_expired``
        audit event so a sibling can later observe the hand-off.

        :param run_id: The run to release.
        :param lease_owner: The current holder. Mismatched callers
            get :class:`IssueRunConflictError`.
        :raises IssueRunConflictError: When a different lease_owner
            holds the lease.
        """

    @abstractmethod
    def transition(
        self,
        run_id: str,
        *,
        to_state: str,
        lease_owner: str | None = None,
        patch: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
        event_kind: str = "state_transition",
    ) -> IssueRun:
        """Advance *run_id* from its current state to *to_state*.

        Writes an :class:`IssueRunEvent` capturing the transition so
        a re-load from events reconstructs the exact run state.

        :param run_id: The run to transition.
        :param to_state: Target state. Must be on the legal-edge
            graph for the current state (see
            :func:`omnigent.entities.issue_run.is_legal_state_edge`).
        :param lease_owner: If provided, the transition is rejected
            when a different caller now holds the lease. ``None``
            means "no lease check" (used by the reap / re-claim
            sweep).
        :param patch: Optional column patches applied atomically
            with the transition (e.g. ``branch``, ``head_sha``,
            ``pr_number``). Only whitelisted columns are writable;
            illegal keys raise :class:`ValueError`.
        :param event_payload: Free-form dict serialized to JSON and
            stored on the event row.
        :param event_kind: One of :data:`ISSUE_RUN_EVENT_KINDS`. The
            store rejects unknown kinds.
        :returns: The persisted :class:`IssueRun` after the
            transition.
        :raises IssueRunStateError: When the transition is not on
            the legal-edge graph.
        :raises IssueRunConflictError: When ``lease_owner`` is
            supplied and doesn't match the current holder.
        :raises ValueError: When ``event_kind`` is unknown or
            ``patch`` contains a non-whitelisted key.
        """

    @abstractmethod
    def reap_expired_leases(self) -> list[IssueRun]:
        """Find rows whose lease has expired and mark them abandoned.

        Called periodically (the recovery sweep on deployment
        startup, plus a background timer inside the autopilot
        scheduler). Each reaped row emits ``claim_expired`` +
        ``abandoned`` events and is moved to
        :data:`IssueRunState.ABANDONED` so a sibling can claim
        the issue in a fresh run.

        :returns: The list of rows that were reaped in this call.
        """

    @abstractmethod
    def create_follow_up(
        self,
        *,
        predecessor_id: str,
        repository: str,
        issue_number: int,
        parent_session_id: str | None = None,
        lease_ttl_s: float | None = None,
    ) -> IssueRun:
        """Create a fresh run that supersedes a terminal *predecessor_id*.

        Used when the autopilot wants to retry a failed / abandoned
        issue without inheriting the predecessor's audit log. The
        predecessor stays in its terminal state; the new row
        starts at :data:`IssueRunState.QUEUED`.

        :param predecessor_id: The terminal run id being
            superseded. Stored only as an audit breadcrumb on the
            first event; not a foreign key.
        :param repository: ``owner/repo`` slug.
        :param issue_number: The issue the predecessor failed on.
        :param parent_session_id: Optional parent harness session.
        :param lease_ttl_s: Override for the lease TTL.
        :returns: The new :class:`IssueRun`.
        """


# Whitelisted patch keys for ``IssueRunStore.transition``. Keeps the
# SQL UPDATE column-list closed so callers can't accidentally write
# to a column the contract reserves for the store itself.
ALLOWED_TRANSITION_PATCH_KEYS: frozenset[str] = frozenset(
    {
        "parent_session_id",
        "worker_session_id",
        "branch",
        "worktree",
        "head_sha",
        "pr_number",
        "review_iteration",
        "retry_count",
        "last_error",
        "last_error_code",
    }
)


def _validate_event_kind(kind: str) -> None:
    """Raise :class:`ValueError` if *kind* is not a known event kind."""
    if kind not in ISSUE_RUN_EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind!r}")


def _validate_state_edge(from_state: str, to_state: str) -> None:
    """Raise :class:`IssueRunStateError` if the edge is not legal."""
    if not is_legal_state_edge(from_state, to_state):
        raise IssueRunStateError(run_id="<unknown>", from_state=from_state, to_state=to_state)


def _validate_patch_keys(patch: dict[str, Any] | None) -> None:
    """Reject patches that touch non-whitelisted columns."""
    if not patch:
        return
    bad = set(patch.keys()) - ALLOWED_TRANSITION_PATCH_KEYS
    if bad:
        raise ValueError(f"patch keys not allowed: {sorted(bad)!r}")


def _coerce_payload(payload: dict[str, Any] | None) -> str | None:
    """Serialize a dict payload to compact JSON for the events column."""
    if payload is None:
        return None
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


# Importing the SQLAlchemy implementation at module top would create a
# circular import (the impl imports ``IssueRunStore`` from this package
# during type-checking). Defer it until ``__all__`` is defined and
# re-export it explicitly so ``from omnigent.stores.issue_run_store
# import SqlIssueRunStore`` works for callers.

__all__ = [
    "ALLOWED_TRANSITION_PATCH_KEYS",
    "DEFAULT_ISSUE_RUN_LEASE_S",
    "ISSUE_RUN_EVENT_KINDS",
    "ISSUE_RUN_STATE_EDGES",
    "IssueRun",
    "IssueRunConflictError",
    "IssueRunEvent",
    "IssueRunState",
    "IssueRunStateError",
    "IssueRunStore",
    "SqlIssueRunStore",
    "_coerce_payload",
    "_lease_ttl_s",
    "_new_run_id",
    "_now_epoch",
    "_validate_event_kind",
    "_validate_patch_keys",
    "_validate_state_edge",
    "is_legal_state_edge",
]

from omnigent.stores.issue_run_store.sqlalchemy_store import SqlIssueRunStore  # noqa: E402


