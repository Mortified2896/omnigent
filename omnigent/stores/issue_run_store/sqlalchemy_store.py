"""SQLAlchemy-backed :class:`IssueRunStore`.

The lease + state machine are the heart of issue #18's atomic-claim
contract. Every method that mutates ``lease_owner`` /
``lease_expires_at`` or ``state`` runs inside a single
``engine.begin()`` block so the database is the linearisation point
and two concurrent runners can never both hold the same run.

Implementation notes:

- :meth:`SqlIssueRunStore.try_claim` runs an INSERT-or-UPDATE inside
  one transaction. The decision tree:
  1. ``SELECT ... FOR UPDATE`` to acquire a row lock on the
     (workspace, repository, issue) tuple. ``FOR UPDATE`` is a
     Postgres / MySQL feature; on SQLite (which is what tests use)
     we rely on the per-process write lock the transaction already
     holds. The lock is released on commit.
  2. If no row exists: INSERT a new row in ``QUEUED`` state with
     the lease populated.
  3. If a row exists and is terminal (``DONE`` / ``FAILED`` /
     ``ABANDONED``): raise :class:`IssueRunConflictError` so the
     caller can use :meth:`create_follow_up`.
  4. If a row exists and the lease has expired (``lease_expires_at
     < now``): UPDATE the lease columns + emit
     ``claim_recovered``. Stay in whatever state the row is in so
     the recovery sweep can resume mid-workflow.
  5. Otherwise: raise :class:`IssueRunConflictError`.
- :meth:`SqlIssueRunStore.transition` runs ``SELECT ... FOR UPDATE``
  to lock the row, validates the legal-edge graph, applies the
  patch + state change + emits an event row, all in one
  transaction.
- :meth:`SqlIssueRunStore.reap_expired_leases` is the recovery
  sweep entry point. It scans ``ix_issue_runs_state_lease`` for
  expired leases, marks each row ``ABANDONED``, and emits the
  ``claim_expired`` + ``abandoned`` events.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlIssueRun,
    SqlIssueRunEvent,
    current_workspace_id,
)
from omnigent.db.enum_codecs import (
    decode_issue_run_state,
    encode_issue_run_state,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
)
from omnigent.entities import IssueRun, IssueRunEvent, IssueRunState
from omnigent.stores.issue_run_store import (
    IssueRunConflictError,
    IssueRunStore,
    _coerce_payload,
    _lease_ttl_s,
    _new_run_id,
    _now_epoch,
    _validate_event_kind,
    _validate_patch_keys,
    _validate_state_edge,
)

# State codes considered "non-terminal" by the recovery sweep +
# claim path. Matches :data:`omnigent.entities.issue_run.IssueRunState`'s
# terminal set (``DONE`` / ``FAILED`` / ``ABANDONED``).
_NON_TERMINAL_STATES: tuple[str, ...] = (
    IssueRunState.QUEUED.value,
    IssueRunState.CLAIMING.value,
    IssueRunState.CLAIMED.value,
    IssueRunState.IN_PROGRESS.value,
    IssueRunState.PR_READY.value,
)


def _to_entity(row: SqlIssueRun) -> IssueRun:
    """Convert an :class:`SqlIssueRun` ORM row to an :class:`IssueRun`."""
    return IssueRun(
        id=row.id,
        repository=row.repository,
        issue_number=row.issue_number,
        state=decode_issue_run_state(row.state),
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        lease_owner=row.lease_owner,
        lease_acquired_at=row.lease_acquired_at,
        lease_expires_at=row.lease_expires_at,
        parent_session_id=row.parent_session_id,
        worker_session_id=row.worker_session_id,
        branch=row.branch,
        worktree=row.worktree,
        head_sha=row.head_sha,
        pr_number=row.pr_number,
        review_iteration=row.review_iteration,
        retry_count=row.retry_count,
        last_error=row.last_error,
        last_error_code=row.last_error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_event_entity(row: SqlIssueRunEvent) -> IssueRunEvent:
    """Convert an :class:`SqlIssueRunEvent` ORM row to an :class:`IssueRunEvent`.

    The ``payload`` column stores JSON text; the entity surfaces it
    as the raw JSON string the caller can deserialize. We keep the
    raw string instead of decoding here because event payloads can
    be any shape and the store / API can decide how to render.
    """
    return IssueRunEvent(
        id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        kind=row.kind,
        from_state=row.from_state,
        to_state=row.to_state,
        payload=row.payload,
        created_at=row.created_at,
    )


class SqlIssueRunStore(IssueRunStore):
    """SQLAlchemy-backed implementation of the issue-run store.

    Backed by ``issue_runs`` + ``issue_run_events`` tables; the
    ``storage_location`` ctor argument is a SQLAlchemy URL accepted
    by :func:`omnigent.db.utils.get_or_create_engine`.
    """

    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session_factory = make_managed_session_maker(self._engine, immediate=True)

    # ── Reads ────────────────────────────────────────────────

    def get_by_run_id(self, run_id: str) -> IssueRun | None:
        """Return the run with id *run_id*, or ``None``."""
        with self._session_factory() as session:
            row = session.get(SqlIssueRun, (current_workspace_id(), run_id))
            return _to_entity(row) if row is not None else None

    def get_active(self, repository: str, issue_number: int) -> IssueRun | None:
        """Return the non-terminal run for *(repository, issue_number)*.

        The V1 contract allows at most one active run per issue, so
        the SELECT filters on the non-terminal state set and
        returns the most recent (``updated_at`` desc) match.
        """
        with self._session_factory() as session:
            stmt = (
                select(SqlIssueRun)
                .where(
                    SqlIssueRun.workspace_id == current_workspace_id(),
                    SqlIssueRun.repository == repository,
                    SqlIssueRun.issue_number == issue_number,
                    SqlIssueRun.state.in_(
                        [encode_issue_run_state(s) for s in _NON_TERMINAL_STATES]
                    ),
                )
                .order_by(SqlIssueRun.updated_at.desc())
                .limit(1)
            )
            row = session.execute(stmt).scalar_one_or_none()
            return _to_entity(row) if row is not None else None

    def list_active(self) -> list[IssueRun]:
        """Return every row in a non-terminal state."""
        with self._session_factory() as session:
            stmt = (
                select(SqlIssueRun)
                .where(
                    SqlIssueRun.workspace_id == current_workspace_id(),
                    SqlIssueRun.state.in_(
                        [encode_issue_run_state(s) for s in _NON_TERMINAL_STATES]
                    ),
                )
                .order_by(SqlIssueRun.updated_at.desc())
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(row) for row in rows]

    def list_events(self, run_id: str) -> list[IssueRunEvent]:
        """Return every event for *run_id* in sequence order."""
        with self._session_factory() as session:
            stmt = (
                select(SqlIssueRunEvent)
                .where(
                    SqlIssueRunEvent.workspace_id == current_workspace_id(),
                    SqlIssueRunEvent.run_id == run_id,
                )
                .order_by(SqlIssueRunEvent.sequence.asc())
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_event_entity(row) for row in rows]

    # ── Writes ───────────────────────────────────────────────

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

        See module docstring for the decision tree. The whole flow
        runs in one ``engine.begin()`` transaction; the SELECT
        locks the natural-key tuple (``workspace_id, repository,
        issue_number``) for the duration of the claim so a sibling
        runner waiting on the same lock will see the new lease
        after we commit.
        """
        owner = lease_owner or _new_run_id()
        ttl = lease_ttl_s if lease_ttl_s is not None else _lease_ttl_s()
        now = _now_epoch()
        with self._session_factory() as session:
            workspace_id = current_workspace_id()
                # session is already inside a transaction (managed_session with immediate=True).
                # Lock the natural-key tuple. ``FOR UPDATE`` is a no-op
                # on SQLite but the per-transaction write lock keeps
            # the logic identical; on Postgres / MySQL it
            # serialises concurrent claim attempts.
            stmt = (
                select(SqlIssueRun)
                .where(
                    SqlIssueRun.workspace_id == workspace_id,
                    SqlIssueRun.repository == repository,
                    SqlIssueRun.issue_number == issue_number,
                )
                .with_for_update()
            )
            existing = session.execute(stmt).scalar_one_or_none()
            if existing is None:
                run_id = _new_run_id()
                session.add(
                    SqlIssueRun(
                        workspace_id=workspace_id,
                        id=run_id,
                        repository=repository,
                        issue_number=issue_number,
                        state=encode_issue_run_state(IssueRunState.QUEUED.value),
                        lease_owner=owner,
                        lease_acquired_at=now,
                        lease_expires_at=now + int(ttl),
                        parent_session_id=parent_session_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                # ``created`` is the first event; the legal-edge
                # transition from ``None`` (creation) is implicit
                # in the row insert.
                session.add(
                    SqlIssueRunEvent(
                        workspace_id=workspace_id,
                        id=_new_run_id(),
                        run_id=run_id,
                        sequence=1,
                        kind="created",
                        from_state=None,
                        to_state=IssueRunState.QUEUED.value,
                        payload=_coerce_payload(
                            {
                                "repository": repository,
                                "issue_number": issue_number,
                                "lease_owner": owner,
                            }
                        ),
                        created_at=now,
                    )
                )
                session.flush()
                row = session.execute(
                    select(SqlIssueRun).where(SqlIssueRun.id == run_id)
                ).scalar_one()
                return _to_entity(row)
            state_name = decode_issue_run_state(existing.state)
            if state_name not in _NON_TERMINAL_STATES:
                raise IssueRunConflictError(
                    repository=repository,
                    issue_number=issue_number,
                    message=(
                        f"existing run {existing.id} is in terminal state "
                        f"{state_name!r}; use create_follow_up to retry"
                    ),
                )
            # Non-terminal — is the lease still live?
            lease_live = (
                existing.lease_expires_at is not None
                and existing.lease_expires_at > now
            )
            if lease_live and existing.lease_owner != owner:
                raise IssueRunConflictError(
                    repository=repository,
                    issue_number=issue_number,
                    message=(
                        f"existing run {existing.id} is held by "
                        f"lease_owner={existing.lease_owner!r} "
                        f"until lease_expires_at={existing.lease_expires_at}"
                    ),
                )
            if lease_live:
                # Same caller — extend the lease and return the
                # existing row (idempotent claim).
                existing.lease_expires_at = now + int(ttl)
                existing.lease_acquired_at = now
                existing.updated_at = now
                session.flush()
                row = session.execute(
                    select(SqlIssueRun).where(SqlIssueRun.id == existing.id)
                ).scalar_one()
                return _to_entity(row)
            # Expired lease — take it.
            sequence = _next_sequence(session, existing.id)
            existing.lease_owner = owner
            existing.lease_acquired_at = now
            existing.lease_expires_at = now + int(ttl)
            existing.updated_at = now
            if parent_session_id is not None:
                existing.parent_session_id = parent_session_id
            session.add(
                SqlIssueRunEvent(
                    workspace_id=workspace_id,
                    id=_new_run_id(),
                    run_id=existing.id,
                    sequence=sequence,
                    kind="claim_recovered",
                    from_state=state_name,
                    to_state=state_name,
                    payload=_coerce_payload(
                        {
                            "lease_owner": owner,
                            "previous_lease_owner": existing.lease_owner,
                            "lease_expires_at": existing.lease_expires_at,
                        }
                    ),
                    created_at=now,
                )
            )
            session.flush()
            row = session.execute(
                select(SqlIssueRun).where(SqlIssueRun.id == existing.id)
            ).scalar_one()
            return _to_entity(row)

    def extend_lease(
        self,
        run_id: str,
        *,
        lease_owner: str,
        lease_ttl_s: float | None = None,
    ) -> IssueRun:
        """Refresh the lease on *run_id* iff *lease_owner* still holds it."""
        ttl = lease_ttl_s if lease_ttl_s is not None else _lease_ttl_s()
        now = _now_epoch()
        with self._session_factory() as session:
            workspace_id = current_workspace_id()
                # session is already inside a transaction (managed_session with immediate=True).
            row = (
                session.execute(
                    select(SqlIssueRun)
                    .where(
                        SqlIssueRun.workspace_id == workspace_id,
                        SqlIssueRun.id == run_id,
                    )
                    .with_for_update()
                )
                .scalar_one_or_none()
            )
            if row is None:
                raise IssueRunConflictError(
                    repository="<unknown>",
                    issue_number=0,
                    message=f"run {run_id!r} does not exist",
                )
            if row.lease_owner != lease_owner:
                raise IssueRunConflictError(
                    repository=row.repository,
                    issue_number=row.issue_number,
                    message=(
                        f"lease for {run_id!r} is held by "
                        f"{row.lease_owner!r}; refresh by "
                        f"{lease_owner!r} rejected"
                    ),
                )
            row.lease_expires_at = now + int(ttl)
            row.lease_acquired_at = now
            row.updated_at = now
            session.add(
                SqlIssueRunEvent(
                    workspace_id=workspace_id,
                    id=_new_run_id(),
                    run_id=row.id,
                    sequence=_next_sequence(session, row.id),
                    kind="claim_extended",
                    from_state=decode_issue_run_state(row.state),
                    to_state=decode_issue_run_state(row.state),
                    payload=_coerce_payload(
                        {
                            "lease_owner": lease_owner,
                            "lease_expires_at": row.lease_expires_at,
                        }
                    ),
                    created_at=now,
                )
            )
            session.flush()
            return _to_entity(row)

    def release_lease(self, run_id: str, *, lease_owner: str) -> None:
        """Release the lease on *run_id* iff *lease_owner* holds it."""
        now = _now_epoch()
        with self._session_factory() as session:
            workspace_id = current_workspace_id()
                # session is already inside a transaction (managed_session with immediate=True).
            row = (
                session.execute(
                    select(SqlIssueRun)
                    .where(
                        SqlIssueRun.workspace_id == workspace_id,
                        SqlIssueRun.id == run_id,
                    )
                    .with_for_update()
                )
                .scalar_one_or_none()
            )
            if row is None:
                raise IssueRunConflictError(
                    repository="<unknown>",
                    issue_number=0,
                    message=f"run {run_id!r} does not exist",
                )
            if row.lease_owner != lease_owner:
                raise IssueRunConflictError(
                    repository=row.repository,
                    issue_number=row.issue_number,
                    message=(
                        f"lease for {run_id!r} is held by "
                        f"{row.lease_owner!r}; release by "
                        f"{lease_owner!r} rejected"
                    ),
                )
            previous_owner = row.lease_owner
            row.lease_owner = None
            row.lease_acquired_at = None
            row.lease_expires_at = None
            row.updated_at = now
            session.add(
                SqlIssueRunEvent(
                    workspace_id=workspace_id,
                    id=_new_run_id(),
                    run_id=row.id,
                    sequence=_next_sequence(session, row.id),
                    kind="claim_expired",
                    from_state=decode_issue_run_state(row.state),
                    to_state=decode_issue_run_state(row.state),
                    payload=_coerce_payload(
                        {"previous_lease_owner": previous_owner, "reason": "released"}
                    ),
                    created_at=now,
                )
            )

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
        """Advance *run_id* to *to_state*, writing an event row.

        See :meth:`IssueRunStore.transition` for the contract.
        """
        _validate_event_kind(event_kind)
        _validate_patch_keys(patch)
        patch = dict(patch or {})
        now = _now_epoch()
        with self._session_factory() as session:
            workspace_id = current_workspace_id()
                # session is already inside a transaction (managed_session with immediate=True).
            row = (
                session.execute(
                    select(SqlIssueRun)
                    .where(
                        SqlIssueRun.workspace_id == workspace_id,
                        SqlIssueRun.id == run_id,
                    )
                    .with_for_update()
                )
                .scalar_one_or_none()
            )
            if row is None:
                raise IssueRunConflictError(
                    repository="<unknown>",
                    issue_number=0,
                    message=f"run {run_id!r} does not exist",
                )
            if lease_owner is not None and row.lease_owner != lease_owner:
                raise IssueRunConflictError(
                    repository=row.repository,
                    issue_number=row.issue_number,
                    message=(
                        f"lease for {run_id!r} is held by "
                        f"{row.lease_owner!r}; transition by "
                        f"{lease_owner!r} rejected"
                    ),
                )
            from_state = decode_issue_run_state(row.state)
            _validate_state_edge(from_state, to_state)
            # Apply the patch against the row, then advance state.
            for key, value in patch.items():
                setattr(row, key, value)
            row.state = encode_issue_run_state(to_state)
            row.updated_at = now
            session.add(
                SqlIssueRunEvent(
                    workspace_id=workspace_id,
                    id=_new_run_id(),
                    run_id=row.id,
                    sequence=_next_sequence(session, row.id),
                    kind=event_kind,
                    from_state=from_state,
                    to_state=to_state,
                    payload=_coerce_payload(event_payload),
                    created_at=now,
                )
            )
            session.flush()
            return _to_entity(row)

    def reap_expired_leases(self) -> list[IssueRun]:
        """Find rows whose lease has expired and mark them abandoned."""
        now = _now_epoch()
        reaped: list[IssueRun] = []
        with self._session_factory() as session:
            workspace_id = current_workspace_id()
                # session is already inside a transaction (managed_session with immediate=True).
            stmt = (
                select(SqlIssueRun)
                .where(
                    SqlIssueRun.workspace_id == workspace_id,
                    SqlIssueRun.state.in_(
                        [encode_issue_run_state(s) for s in _NON_TERMINAL_STATES]
                    ),
                    SqlIssueRun.lease_expires_at.is_not(None),
                    SqlIssueRun.lease_expires_at < now,
                )
                .order_by(SqlIssueRun.lease_expires_at.asc())
                .with_for_update()
            )
            rows = list(session.execute(stmt).scalars().all())
            for row in rows:
                from_state = decode_issue_run_state(row.state)
                previous_owner = row.lease_owner
                row.lease_owner = None
                row.lease_acquired_at = None
                row.lease_expires_at = None
                row.state = encode_issue_run_state(IssueRunState.ABANDONED.value)
                row.updated_at = now
                sequence = _next_sequence(session, row.id)
                session.add(
                    SqlIssueRunEvent(
                        workspace_id=workspace_id,
                        id=_new_run_id(),
                        run_id=row.id,
                        sequence=sequence,
                        kind="claim_expired",
                        from_state=from_state,
                        to_state=IssueRunState.ABANDONED.value,
                        payload=_coerce_payload(
                            {
                                "previous_lease_owner": previous_owner,
                                "reason": "lease_expires_at passed",
                            }
                        ),
                        created_at=now,
                    )
                )
                reaped.append(_to_entity(row))
            session.flush()
        return reaped

    def create_follow_up(
        self,
        *,
        predecessor_id: str,
        repository: str,
        issue_number: int,
        parent_session_id: str | None = None,
        lease_ttl_s: float | None = None,
    ) -> IssueRun:
        """Create a fresh run that supersedes a terminal predecessor."""
        ttl = lease_ttl_s if lease_ttl_s is not None else _lease_ttl_s()
        now = _now_epoch()
        new_id = _new_run_id()
        owner = _new_run_id()
        with self._session_factory() as session:
            workspace_id = current_workspace_id()
                # session is already inside a transaction (managed_session with immediate=True).
            session.add(
                SqlIssueRun(
                    workspace_id=workspace_id,
                    id=new_id,
                    repository=repository,
                    issue_number=issue_number,
                    state=encode_issue_run_state(IssueRunState.QUEUED.value),
                    lease_owner=owner,
                    lease_acquired_at=now,
                    lease_expires_at=now + int(ttl),
                    parent_session_id=parent_session_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                SqlIssueRunEvent(
                    workspace_id=workspace_id,
                    id=_new_run_id(),
                    run_id=new_id,
                    sequence=1,
                    kind="created",
                    from_state=None,
                    to_state=IssueRunState.QUEUED.value,
                    payload=_coerce_payload(
                        {
                            "predecessor_id": predecessor_id,
                            "repository": repository,
                            "issue_number": issue_number,
                            "lease_owner": owner,
                        }
                    ),
                    created_at=now,
                )
            )
            session.flush()
            row = session.execute(
                select(SqlIssueRun).where(SqlIssueRun.id == new_id)
            ).scalar_one()
            return _to_entity(row)


def _next_sequence(session: Any, run_id: str) -> int:
    """Return the next monotonic sequence number for *run_id*.

    The store always reuses the highest existing sequence + 1 so
    event ordering matches :meth:`list_events`. The lock on the
    parent row (held by the caller's ``with session.begin():``)
    makes this safe even under concurrent transitions.
    """
    row = session.execute(
        select(SqlIssueRunEvent.sequence)
        .where(
            SqlIssueRunEvent.run_id == run_id,
        )
        .order_by(SqlIssueRunEvent.sequence.desc())
        .limit(1)
    ).first()
    return (row[0] + 1) if row is not None else 1


__all__ = ["SqlIssueRunStore"]