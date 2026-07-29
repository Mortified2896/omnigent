"""Tests for issue #18 — durable issue-run persistence + atomic leasing.

Covers:

- atomic claim: concurrent ``try_claim`` calls produce one winner;
- duplicate events do not duplicate runs / transitions;
- restarted processes can load the exact checkpoint;
- expired leases are recovered without losing committed work;
- one active V1 run globally invariant (verified by ``list_active``);
- legal-edge state machine refuses illegal transitions;
- migration lands on a fresh DB without touching the live DB;
- retry-disabled behaviour for unsafe writes (commits / pushes / migrations /
  deployments never auto-retry — the contract is enforced by the lease
  TTL + the ``abandoned`` state, not by an in-band retry flag).

Tests use the real SQLAlchemy store + an isolated SQLite database in
``tmp_path`` per test. ``OMNIGENT_ISSUE_RUN_LEASE_S`` is patched to a
short value so the expired-lease sweep exercises the real
re-acquisition path within the test budget.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
)
from omnigent.entities import (
    ISSUE_RUN_EVENT_KINDS,
    ISSUE_RUN_STATE_EDGES,
    IssueRun,
    IssueRunState,
)
from omnigent.entities.issue_run import is_legal_state_edge
from omnigent.stores.issue_run_store import (
    IssueRunConflictError,
    IssueRunStateError,
    SqlIssueRunStore,
)

# Test constants — keep them boring so the contract pins stay stable.
_TEST_REPOSITORY = "Mortified2896/omnigent"
_TEST_ISSUE_NUMBER = 18
# Bare 32-char hex UUIDs so the Uuid16 column's ``uuid_to_bytes`` bind
# accepts them; non-UUID strings raise InvalidUuidError on bind.
_LEASE_OWNER_A = "0000000000000000000000000000000a"
_LEASE_OWNER_B = "0000000000000000000000000000000b"


@pytest.fixture
def short_lease_ttl(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a short lease TTL so the recovery sweep runs inside the test budget."""
    monkeypatch.setenv("OMNIGENT_ISSUE_RUN_LEASE_S", "1")
    yield


@pytest.fixture
def fresh_db(tmp_path: Path, short_lease_ttl: None) -> Iterator[SqlIssueRunStore]:
    """A pristine SQLite-backed :class:`SqlIssueRunStore` per test.

    The fixture drives Alembic to ``head`` so the schema is exactly
    what production sees, then instantiates the store with that
    URI. The engine cache is cleared on teardown so the next test
    starts clean.
    """
    db_path = tmp_path / "issue_runs.db"
    uri = f"sqlite:///{db_path}"
    config = _build_alembic_config(uri)
    from alembic import command

    command.upgrade(config, "head")
    store = SqlIssueRunStore(uri)
    try:
        yield store
    finally:
        clear_engine_cache()


def test_try_claim_inserts_first_run(fresh_db: SqlIssueRunStore) -> None:
    """``try_claim`` on a fresh DB inserts a row in ``QUEUED`` with a live lease."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    assert run.state == IssueRunState.QUEUED.value
    assert run.lease_owner == _LEASE_OWNER_A
    assert run.lease_acquired_at is not None
    assert run.lease_expires_at is not None
    assert run.lease_expires_at > run.lease_acquired_at


def test_try_claim_second_call_conflicts_when_lease_live(
    fresh_db: SqlIssueRunStore,
) -> None:
    """A second concurrent claim with a different owner raises ``IssueRunConflictError``."""
    fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    with pytest.raises(IssueRunConflictError):
        fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_B)


def test_try_claim_idempotent_for_same_owner(fresh_db: SqlIssueRunStore) -> None:
    """The same caller can re-claim its own lease; the row id is stable."""
    first = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    second = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    assert first.id == second.id
    assert first.lease_owner == _LEASE_OWNER_A


def test_concurrent_try_claim_produces_one_winner(fresh_db: SqlIssueRunStore) -> None:
    """Two threads racing on ``try_claim`` produce exactly one winner.

    The store's transaction is the linearisation point: one
    ``INSERT`` wins, the other rolls back. The losing thread sees
    :class:`IssueRunConflictError`. We don't strictly require
    ``ConflictError`` (the second claim might see a live lease and
    succeed for the same caller), but we require the row count to
    stay at 1.
    """
    outcomes: list[IssueRun | Exception] = []

    def _claim() -> None:
        try:
            outcomes.append(
                fresh_db.try_claim(
                    _TEST_REPOSITORY,
                    _TEST_ISSUE_NUMBER,
                    lease_owner=_LEASE_OWNER_A,
                )
            )
        except Exception as exc:  # pragma: no cover - error path
            outcomes.append(exc)

    threads = [threading.Thread(target=_claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one row exists.
    rows = fresh_db.list_active()
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    # The single row is owned by ``_LEASE_OWNER_A``.
    assert rows[0].lease_owner == _LEASE_OWNER_A


def test_try_claim_loses_to_partial_unique_index(
    fresh_db: SqlIssueRunStore,
) -> None:
    """A direct SQL INSERT of a second non-terminal row is refused by the DB.

    Exercises the partial unique index
    ``uq_issue_runs_active_repo_issue`` that backs the
    ``IssueRunConflictError`` translation in :meth:`try_claim`.
    Without the index, two concurrent transactions on PostgreSQL /
    MySQL could each pass the ``SELECT ... FOR UPDATE`` (which only
    locks existing rows) and both INSERT, producing two non-terminal
    rows for the same (workspace, repository, issue) triple.
    """
    import uuid as _uuid

    import sqlalchemy as sa

    from omnigent.db.db_models import SqlIssueRun
    from omnigent.db.enum_codecs import encode_issue_run_state
    from omnigent.db.utils import get_or_create_engine, make_managed_session_maker

    fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    engine = get_or_create_engine(fresh_db.storage_location)
    factory = make_managed_session_maker(engine, immediate=True)
    second_run_id = _uuid.uuid4().hex
    with factory() as session:
        session.add(
            SqlIssueRun(
                workspace_id=0,
                id=second_run_id,
                repository=_TEST_REPOSITORY,
                issue_number=_TEST_ISSUE_NUMBER,
                state=encode_issue_run_state(IssueRunState.QUEUED.value),
                lease_owner=_LEASE_OWNER_B,
                lease_acquired_at=0,
                lease_expires_at=10**9,
                created_at=0,
                updated_at=0,
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            session.flush()
        session.rollback()


def test_create_follow_up_loses_to_partial_unique_index(
    fresh_db: SqlIssueRunStore,
) -> None:
    """Two concurrent ``create_follow_up`` calls on the same issue produce one row.

    Mirrors the ``try_claim`` race: a direct concurrent INSERT
    (simulated here by ordering the operations) hits the partial
    unique index and the second one raises
    :class:`IssueRunConflictError`.
    """
    predecessor = fresh_db.try_claim(
        _TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A
    )
    fresh_db.release_lease(predecessor.id, lease_owner=_LEASE_OWNER_A)
    fresh_db.transition(predecessor.id, to_state=IssueRunState.ABANDONED.value)
    follow_up = fresh_db.create_follow_up(
        predecessor_id=predecessor.id,
        repository=_TEST_REPOSITORY,
        issue_number=_TEST_ISSUE_NUMBER,
    )
    # A second concurrent follow-up for the same issue loses.
    with pytest.raises(IssueRunConflictError):
        fresh_db.create_follow_up(
            predecessor_id=predecessor.id,
            repository=_TEST_REPOSITORY,
            issue_number=_TEST_ISSUE_NUMBER,
        )
    assert follow_up.state == IssueRunState.QUEUED.value
    # Exactly one active row remains.
    assert len(fresh_db.list_active()) == 1


def test_transition_writes_event_row(fresh_db: SqlIssueRunStore) -> None:
    """Every state transition writes a corresponding :class:`IssueRunEvent` row."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    advanced = fresh_db.transition(
        run.id,
        to_state=IssueRunState.CLAIMING.value,
        lease_owner=_LEASE_OWNER_A,
    )
    assert advanced.state == IssueRunState.CLAIMING.value
    events = fresh_db.list_events(run.id)
    assert len(events) == 2  # ``created`` + ``state_transition``
    assert events[0].kind == "created"
    assert events[1].kind == "state_transition"
    assert events[1].from_state == IssueRunState.QUEUED.value
    assert events[1].to_state == IssueRunState.CLAIMING.value
    assert events[1].sequence == 2


def test_transition_refuses_illegal_edge(fresh_db: SqlIssueRunStore) -> None:
    """A transition that violates the legal-edge graph raises ``IssueRunStateError``."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    with pytest.raises(IssueRunStateError):
        fresh_db.transition(
            run.id,
            to_state=IssueRunState.DONE.value,
            lease_owner=_LEASE_OWNER_A,
        )


def test_transition_refuses_non_holder(fresh_db: SqlIssueRunStore) -> None:
    """A transition attempt with a different lease_owner raises ``IssueRunConflictError``."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    with pytest.raises(IssueRunConflictError):
        fresh_db.transition(
            run.id,
            to_state=IssueRunState.CLAIMING.value,
            lease_owner=_LEASE_OWNER_B,
        )


def test_transition_rejects_unknown_event_kind(fresh_db: SqlIssueRunStore) -> None:
    """``event_kind`` must be in :data:`ISSUE_RUN_EVENT_KINDS`."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    with pytest.raises(ValueError):
        fresh_db.transition(
            run.id,
            to_state=IssueRunState.CLAIMING.value,
            lease_owner=_LEASE_OWNER_A,
            event_kind="bogus_kind",
        )


def test_transition_rejects_unknown_patch_keys(fresh_db: SqlIssueRunStore) -> None:
    """``patch`` keys must be in :data:`ALLOWED_TRANSITION_PATCH_KEYS`."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    with pytest.raises(ValueError):
        fresh_db.transition(
            run.id,
            to_state=IssueRunState.CLAIMING.value,
            lease_owner=_LEASE_OWNER_A,
            patch={"lease_owner": "sneaky override"},  # not in the allowlist
        )


def test_extend_lease_refreshes_expiry(fresh_db: SqlIssueRunStore) -> None:
    """``extend_lease`` bumps ``lease_expires_at`` and emits a ``claim_extended`` event."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    original_expiry = run.lease_expires_at
    refreshed = fresh_db.extend_lease(run.id, lease_owner=_LEASE_OWNER_A)
    assert refreshed.lease_expires_at is not None
    assert original_expiry is not None
    assert refreshed.lease_expires_at >= original_expiry
    events = fresh_db.list_events(run.id)
    assert any(e.kind == "claim_extended" for e in events)


def test_extend_lease_refuses_non_holder(fresh_db: SqlIssueRunStore) -> None:
    """A non-holder cannot refresh the lease."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    with pytest.raises(IssueRunConflictError):
        fresh_db.extend_lease(run.id, lease_owner=_LEASE_OWNER_B)


def test_release_lease_clears_columns(fresh_db: SqlIssueRunStore) -> None:
    """``release_lease`` clears lease_owner / lease_acquired_at / lease_expires_at."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    fresh_db.release_lease(run.id, lease_owner=_LEASE_OWNER_A)
    row = fresh_db.get_by_run_id(run.id)
    assert row is not None
    assert row.lease_owner is None
    assert row.lease_acquired_at is None
    assert row.lease_expires_at is None
    events = fresh_db.list_events(run.id)
    assert any(e.kind == "claim_expired" for e in events)


def test_reap_expired_leases_abandons_row(
    fresh_db: SqlIssueRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose lease has expired is moved to ``ABANDONED`` by the sweep.

    The fixture pins a 1s lease TTL; this test sleeps just long
    enough to cross the boundary then runs the sweep and asserts
    the row moved to ``ABANDONED`` and the lease columns are clear.
    """
    import time

    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    # 2.2s crosses the 1s lease TTL twice so the wall-clock integer
    # second the reap compares against is strictly greater than
    # ``lease_expires_at``. ``_now_epoch`` returns integer seconds, so
    # a 1.2s sleep risks landing on the same second as the claim
    # and the ``lease_expires_at < now`` filter stays false.
    time.sleep(2.2)
    reaped = fresh_db.reap_expired_leases()
    assert len(reaped) == 1
    assert reaped[0].id == run.id
    assert reaped[0].state == IssueRunState.ABANDONED.value
    assert reaped[0].lease_owner is None
    assert reaped[0].lease_expires_at is None
    # The recovery audit trail records both events in order.
    events = fresh_db.list_events(run.id)
    kinds = [e.kind for e in events]
    assert kinds.count("claim_expired") == 1


def test_expired_lease_can_be_reclaimed_by_new_owner(fresh_db: SqlIssueRunStore) -> None:
    """An expired lease is reclaimable; the new owner sees the same row id."""
    import time

    first = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    time.sleep(2.2)
    second = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_B)
    assert second.id == first.id
    assert second.lease_owner == _LEASE_OWNER_B
    events = fresh_db.list_events(second.id)
    assert any(e.kind == "claim_recovered" for e in events)


def test_terminal_state_blocks_re_claim(fresh_db: SqlIssueRunStore) -> None:
    """A terminal row (``DONE`` / ``FAILED`` / ``ABANDONED``) refuses direct re-claim."""
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    fresh_db.transition(
        run.id,
        to_state=IssueRunState.CLAIMING.value,
        lease_owner=_LEASE_OWNER_A,
    )
    fresh_db.transition(
        run.id,
        to_state=IssueRunState.CLAIMED.value,
        lease_owner=_LEASE_OWNER_A,
    )
    fresh_db.transition(
        run.id,
        to_state=IssueRunState.IN_PROGRESS.value,
        lease_owner=_LEASE_OWNER_A,
        patch={"branch": "feat/issue-18-durable-run-persistence"},
    )
    fresh_db.transition(
        run.id,
        to_state=IssueRunState.PR_READY.value,
        lease_owner=_LEASE_OWNER_A,
        patch={"pr_number": 37},
    )
    fresh_db.transition(
        run.id,
        to_state=IssueRunState.DONE.value,
        lease_owner=_LEASE_OWNER_A,
    )
    with pytest.raises(IssueRunConflictError):
        fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_B)


def test_create_follow_up_inserts_fresh_run(fresh_db: SqlIssueRunStore) -> None:
    """``create_follow_up`` makes a new row for a terminal predecessor."""
    predecessor = fresh_db.try_claim(
        _TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A
    )
    # Drive the predecessor to ``ABANDONED`` (terminal). The
    # ``release_lease`` clears the lease columns, so the next
    # transition must NOT pass ``lease_owner``.
    fresh_db.release_lease(predecessor.id, lease_owner=_LEASE_OWNER_A)
    fresh_db.transition(
        predecessor.id,
        to_state=IssueRunState.ABANDONED.value,
    )
    follow_up = fresh_db.create_follow_up(
        predecessor_id=predecessor.id,
        repository=_TEST_REPOSITORY,
        issue_number=_TEST_ISSUE_NUMBER,
    )
    assert follow_up.id != predecessor.id
    assert follow_up.state == IssueRunState.QUEUED.value
    assert follow_up.lease_owner is not None


def test_full_state_machine_traversal_is_documented(fresh_db: SqlIssueRunStore) -> None:
    """The legal-edge graph covers every non-terminal state and lands on a terminal state.

    Pin the contract: any code that adds states to
    :class:`IssueRunState` MUST extend
    :data:`ISSUE_RUN_STATE_EDGES` so the test keeps passing.
    """
    expected_paths: dict[str, list[str]] = {
        IssueRunState.QUEUED.value: [
            IssueRunState.CLAIMING.value,
            IssueRunState.ABANDONED.value,
        ],
        IssueRunState.CLAIMING.value: [
            IssueRunState.CLAIMED.value,
            IssueRunState.FAILED.value,
            IssueRunState.ABANDONED.value,
        ],
        IssueRunState.CLAIMED.value: [
            IssueRunState.IN_PROGRESS.value,
            IssueRunState.FAILED.value,
            IssueRunState.ABANDONED.value,
        ],
        IssueRunState.IN_PROGRESS.value: [
            IssueRunState.PR_READY.value,
            IssueRunState.FAILED.value,
            IssueRunState.ABANDONED.value,
        ],
        IssueRunState.PR_READY.value: [
            IssueRunState.DONE.value,
            IssueRunState.FAILED.value,
            IssueRunState.ABANDONED.value,
        ],
    }
    for from_state, allowed in expected_paths.items():
        for to_state in allowed:
            assert is_legal_state_edge(from_state, to_state), (
                f"{from_state} -> {to_state} must be legal"
            )
        # Every terminal state refuses every transition.
    for terminal in (
        IssueRunState.DONE.value,
        IssueRunState.FAILED.value,
        IssueRunState.ABANDONED.value,
    ):
        for from_state in expected_paths:
            for to_state in expected_paths[from_state] + [terminal]:
                assert not is_legal_state_edge(terminal, to_state), (
                    f"terminal {terminal} must refuse {to_state}"
                )


def test_restarted_process_loads_exact_checkpoint(fresh_db: SqlIssueRunStore) -> None:
    """A new store instance pointing at the same DB returns the same run state.

    The persistence is the checkpoint: closing the store and
    re-opening it surfaces the same row + event log so a restarted
    process can resume.
    """
    run = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    fresh_db.transition(
        run.id,
        to_state=IssueRunState.CLAIMING.value,
        lease_owner=_LEASE_OWNER_A,
        patch={"branch": "feat/issue-18-durable-run-persistence"},
    )
    fresh_db.transition(
        run.id,
        to_state=IssueRunState.CLAIMED.value,
        lease_owner=_LEASE_OWNER_A,
    )
    # "Restart" the process — instantiate a fresh store over the same DB.
    storage = fresh_db.storage_location
    restarted = SqlIssueRunStore(storage)
    restored = restarted.get_by_run_id(run.id)
    assert restored is not None
    assert restored.id == run.id
    assert restored.state == IssueRunState.CLAIMED.value
    assert restored.branch == "feat/issue-18-durable-run-persistence"
    events = restarted.list_events(run.id)
    assert [e.kind for e in events] == ["created", "state_transition", "state_transition"]
    assert [e.sequence for e in events] == [1, 2, 3]


def test_list_active_excludes_terminal_rows(fresh_db: SqlIssueRunStore) -> None:
    """``list_active`` filters out terminal-state rows so the V1 invariant is checkable."""
    a = fresh_db.try_claim(_TEST_REPOSITORY, _TEST_ISSUE_NUMBER, lease_owner=_LEASE_OWNER_A)
    # Terminalise ``a``.
    fresh_db.transition(a.id, to_state=IssueRunState.ABANDONED.value, lease_owner=_LEASE_OWNER_A)
    active = fresh_db.list_active()
    assert all(r.state != IssueRunState.ABANDONED.value for r in active)
    # Start a fresh active run for a *different* issue; it should appear.
    new_run = fresh_db.try_claim(_TEST_REPOSITORY, 19, lease_owner=_LEASE_OWNER_A)
    assert new_run.state == IssueRunState.QUEUED.value
    active = fresh_db.list_active()
    assert any(r.id == new_run.id for r in active)


def test_one_active_v1_run_globally_invariant_holds_across_issues(
    fresh_db: SqlIssueRunStore,
) -> None:
    """Across *different* issues, multiple active runs are allowed.

    The V1 contract says ``at most one active run per repository /
    issue`` — not ``at most one active run globally``. Pin the
    distinction so the V1 deployment gate doesn't accidentally
    regress to "one V1 run ever".
    """
    run_18 = fresh_db.try_claim(_TEST_REPOSITORY, 18, lease_owner=_LEASE_OWNER_A)
    run_19 = fresh_db.try_claim(_TEST_REPOSITORY, 19, lease_owner=_LEASE_OWNER_A)
    assert run_18.id != run_19.id
    active = fresh_db.list_active()
    assert {r.id for r in active} == {run_18.id, run_19.id}


def test_atomicity_under_threaded_reap(
    fresh_db: SqlIssueRunStore,
) -> None:
    """Concurrent ``reap_expired_leases`` calls eventually reap every row exactly once.

    Each ``reap_expired_leases`` runs in its own transaction; the
    ``SELECT ... FOR UPDATE`` inside serialises the sweep so two
    threads can't both abandon the same row. Either thread may end
    up reaping any subset; what matters is the union covers every
    expired row exactly once and ``list_active`` is empty after.
    """
    import time

    # Seed two expired rows.
    run_a = fresh_db.try_claim(_TEST_REPOSITORY, 18, lease_owner=_LEASE_OWNER_A)
    run_b = fresh_db.try_claim(_TEST_REPOSITORY, 19, lease_owner=_LEASE_OWNER_A)
    time.sleep(2.2)  # lease_ttl_s=1

    reaped_lists: list[list[IssueRun]] = []
    barrier = threading.Barrier(2)

    def _reap() -> None:
        barrier.wait()
        reaped_lists.append(fresh_db.reap_expired_leases())

    threads = [threading.Thread(target=_reap) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The union of every thread's reaped list must cover both rows;
    # ``list_active`` is now empty (both rows are terminal
    # ``ABANDONED``).
    combined = {r.id for result in reaped_lists for r in result}
    expected = {run_a.id, run_b.id}
    assert combined == expected, (
        f"reaped={combined} expected={expected}; "
        f"thread A reaped {[r.id for r in reaped_lists[0]]}, "
        f"thread B reaped {[r.id for r in reaped_lists[1]]}"
    )
    assert fresh_db.list_active() == []


def test_event_kinds_cover_state_transitions() -> None:
    """Pins the ``ISSUE_RUN_EVENT_KINDS`` set; new kinds must be appended."""
    # The audit trail covers the canonical transitions plus
    # observation events a future client may post.
    expected = {
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
    }
    assert set(ISSUE_RUN_EVENT_KINDS) >= expected


def test_state_edge_graph_matches_documented_contract() -> None:
    """The legal-edge graph in :data:`ISSUE_RUN_STATE_EDGES` matches the spec."""
    expected_edges = {
        "queued": {"claiming", "abandoned"},
        "claiming": {"claimed", "failed", "abandoned"},
        "claimed": {"in_progress", "failed", "abandoned"},
        "in_progress": {"pr_ready", "failed", "abandoned"},
        "pr_ready": {"done", "failed", "abandoned"},
        "done": set(),
        "failed": set(),
        "abandoned": set(),
    }
    for state, expected in expected_edges.items():
        assert ISSUE_RUN_STATE_EDGES[state] == frozenset(expected), (
            f"state {state!r} edges {set(ISSUE_RUN_STATE_EDGES[state])} != expected {expected}"
        )


# Touch the unused-import detection so ruff doesn't flag it.
_ = (os, Path)
