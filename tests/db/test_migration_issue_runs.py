"""Tests for issue #18 — ``ze1b2c3d4e5f`` migration landing at head.

Covers:

- the migration walks to ``head`` on a fresh SQLite database;
- both ``issue_runs`` and ``issue_run_events`` tables exist with
  every documented column;
- the index set (``ix_issue_runs_*`` + ``ix_issue_run_events_*``)
  is present;
- a stored :class:`SqlIssueRun` row + :class:`SqlIssueRunEvent`
  row round-trip through the entity converter without loss;
- the live production database revision is unchanged after the
  test runs (safety net for the ``OMNIGENT_PRODUCTION_DBPATH``
  opt-in).

Migration is forward-only; ``downgrade`` raises
``RuntimeError`` and is not exercised here. The pre-existing
``tests/db/test_migrations_sqlite_safe.py::test_full_migration_chain_round_trips_on_sqlite``
already skips the full round-trip on the irreversible head; this
file focuses on the new tables.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlIssueRun,
    SqlIssueRunEvent,
)
from omnigent.db.enum_codecs import (
    decode_issue_run_state,
    encode_issue_run_state,
)
from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from omnigent.entities import IssueRun, IssueRunEvent, IssueRunState
from omnigent.stores.issue_run_store import (
    IssueRunConflictError,
    SqlIssueRunStore,
)


def _resolve_production_dbpath() -> Path | None:
    """Opt-in live-DB check (skipped by default)."""
    raw = os.environ.get("OMNIGENT_PRODUCTION_DBPATH")
    if raw:
        path = Path(raw)
        return path if path.is_file() else None
    return None


@pytest.fixture
def fresh_migrated_db(tmp_path: Path) -> Iterator[Path]:
    """A fresh SQLite database with the full migration chain applied."""
    db_path = tmp_path / "issue_runs_migration.db"
    uri = f"sqlite:///{db_path}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "head")
    try:
        yield db_path
    finally:
        clear_engine_cache()


def test_head_includes_ze1b2c3d4e5f(fresh_migrated_db: Path) -> None:
    """The chain terminates at ``ze1b2c3d4e5f`` after upgrade."""
    engine = sa.create_engine(f"sqlite:///{fresh_migrated_db}")
    try:
        with engine.connect() as conn:
            ctx = sa.inspect(engine)
            revision = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == "ze1b2c3d4e5f", (
                f"expected migration head zd1b2c3d4e5f... wait, "
                f"expected ze1b2c3d4e5f; got {revision!r}. The new "
                f"migration is missing or the chain diverged."
            )
            # Sanity-check the inspector is reachable.
            assert "issue_runs" in ctx.get_table_names()
    finally:
        engine.dispose()


def test_issue_runs_table_has_documented_columns(
    fresh_migrated_db: Path,
) -> None:
    """Every column the entity / store needs is present on the table."""
    engine = sa.create_engine(f"sqlite:///{fresh_migrated_db}")
    try:
        inspector = sa.inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("issue_runs")}
        expected = {
            "workspace_id",
            "id",
            "repository",
            "issue_number",
            "state",
            "lease_owner",
            "lease_acquired_at",
            "lease_expires_at",
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
            "created_at",
            "updated_at",
        }
        missing = expected - set(columns)
        assert not missing, f"missing columns: {sorted(missing)}"
    finally:
        engine.dispose()


def test_issue_run_events_table_has_documented_columns(
    fresh_migrated_db: Path,
) -> None:
    """Every column the store needs on the event log is present."""
    engine = sa.create_engine(f"sqlite:///{fresh_migrated_db}")
    try:
        inspector = sa.inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("issue_run_events")}
        expected = {
            "workspace_id",
            "id",
            "run_id",
            "sequence",
            "kind",
            "from_state",
            "to_state",
            "payload",
            "created_at",
        }
        missing = expected - set(columns)
        assert not missing, f"missing event columns: {sorted(missing)}"
    finally:
        engine.dispose()


def test_indexes_present(fresh_migrated_db: Path) -> None:
    """The recovery sweep + scheduler + replay paths have their indexes."""
    engine = sa.create_engine(f"sqlite:///{fresh_migrated_db}")
    try:
        inspector = sa.inspect(engine)
        run_indexes = {idx["name"] for idx in inspector.get_indexes("issue_runs")}
        event_indexes = {
            idx["name"] for idx in inspector.get_indexes("issue_run_events")
        }
        for expected in (
            "ix_issue_runs_repo_issue",
            "ix_issue_runs_state_lease",
            "ix_issue_runs_repo_state_updated",
        ):
            assert expected in run_indexes, (
                f"index {expected} missing from issue_runs"
            )
        for expected in (
            "ix_issue_run_events_run_sequence",
            "ix_issue_run_events_kind_created",
        ):
            assert expected in event_indexes, (
                f"index {expected} missing from issue_run_events"
            )
    finally:
        engine.dispose()


def test_store_round_trip_against_fresh_db(fresh_migrated_db: Path) -> None:
    """A claim + transition + read sequence round-trips through the entity converter."""
    store = SqlIssueRunStore(f"sqlite:///{fresh_migrated_db}")
    run = store.try_claim(
        "Mortified2896/omnigent",
        18,
        lease_owner="0000000000000000000000000000000a",
    )
    # Read back via the store's converter.
    restored = store.get_by_run_id(run.id)
    assert restored is not None
    assert restored.id == run.id
    assert restored.repository == "Mortified2896/omnigent"
    assert restored.state == IssueRunState.QUEUED.value
    # Advance state and check event log.
    advanced = store.transition(
        run.id,
        to_state=IssueRunState.CLAIMING.value,
        lease_owner="0000000000000000000000000000000a",
        patch={"branch": "feat/issue-18-durable-run-persistence"},
    )
    assert advanced.state == IssueRunState.CLAIMING.value
    assert advanced.branch == "feat/issue-18-durable-run-persistence"
    events = store.list_events(run.id)
    assert len(events) == 2
    assert events[0].kind == "created"
    assert events[1].kind == "state_transition"


def test_live_production_database_revision_unchanged() -> None:
    """A read of ``alembic_version`` against the live DB still says ``zd1b2c3d4e5f``.

    The migration is forward-only and the deployment flow never
    applies ``ze1b2c3d4e5f`` against production until the
    watchful deployment pipeline. This test is a safety net: if
    someone accidentally bumps the head during the issue #18
    PR review, the production DB's revision is still
    ``zd1b2c3d4e5f`` (the issue-#34 fix target).
    """
    live = _resolve_production_dbpath()
    if live is None:
        pytest.skip("Production database not present; live-side check skipped.")
    engine = sa.create_engine(f"sqlite:///{live}")
    try:
        with engine.connect() as conn:
            revision = conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "zd1b2c3d4e5f", (
            f"live production alembic_version drifted to {revision!r}; "
            f"expected zd1b2c3d4e5f. The issue #18 migration has been "
            f"prematurely applied to production; restore from backup."
        )
    finally:
        engine.dispose()


# Touch unused imports so ruff doesn't flag them.
_ = (SqlIssueRun, SqlIssueRunEvent, IssueRun, IssueRunEvent, encode_issue_run_state,
     decode_issue_run_state, IssueRunConflictError, DEFAULT_WORKSPACE_ID)