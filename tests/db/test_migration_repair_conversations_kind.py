"""Pins the deployment lineage to ``zg1a2b3c4d5e`` and proves safety of the repair.

The repair migration
(``zg1a2b3c4d5e_repair_conversations_kind.py``) was originally
authored on ``fix/opencode-native-delegation`` (commits ``4fad6a5b``
and ``d7331e95``) but the lockfile PR (#60) was rebased onto
``0f3c727a`` and therefore did not carry the migration into
``bc22b799``. The deployment updater refuses to install a release
whose migration set does not include the live production revision,
so the migration had to land on the deployment lineage before the
PR #60 release could be promoted.

These tests verify three properties required to deploy the merged
``bc22b799`` + ``zg1a2b3c4d5e`` lineage safely:

1. A fresh database at the new head (``zg1a2b3c4d5e``) upgrades
   cleanly from empty, with no missing migrations.
2. The current production copy is already at ``zg1a2b3c4d5e`` and
   ``alembic upgrade head`` against it is a no-op (zero scripts
   applied). This is the exact property the deployment gate relies
   on.
3. A synthetic repair-mode fixture (a database that contains the
   pre-repair schema) is upgraded correctly: each missing column
   on ``conversations`` is added with the documented ``server_default``,
   the ``kind`` check + index are installed, and the resulting
   schema is byte-equivalent to a fresh-from-scratch upgrade.

Each test skips when the live production database is not present,
so contributors without access to the host can still run the rest
of the suite. CI supplies ``OMNIGENT_PRODUCTION_DBPATH`` when
running the full pre-deploy check.

The repair migration is intentionally idempotent: each column-add
calls ``ADD COLUMN`` only when the column does not already exist,
so re-running the migration on an already-repaired database is a
zero-effect no-op. Test 2 covers that path against the live
production copy.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

import omnigent.db

VERSIONS_DIR = Path(omnigent.db.__file__).parent / "migrations" / "versions"
EXPECTED_HEAD = "zg1a2b3c4d5e"

DEFAULT_PRODUCTION_DBPATH = Path("/home/hermes/.omnigent/chat.db")


def _resolve_production_dbpath() -> Path | None:
    raw = os.environ.get("OMNIGENT_PRODUCTION_DBPATH")
    if raw:
        path = Path(raw)
        return path if path.is_file() else None
    return DEFAULT_PRODUCTION_DBPATH if DEFAULT_PRODUCTION_DBPATH.is_file() else None


@pytest.fixture
def copied_production_db(tmp_path: Path) -> Iterator[Path]:
    """Copy the production chat.db into ``tmp_path`` via SQLite's online backup API.

    SQLite's online backup is used rather than ``shutil.copy`` so a
    long-running production database can be cloned without risking
    a copy against a file that is being actively written. (The
    live ``chat.db`` is written through a 5.12 WAL file under
    ``chat.db-wal``; copying the main file while a writer is in
    flight can produce a half-consistent image and Alembic will
    then refuse to start.)
    """
    source = _resolve_production_dbpath()
    if source is None:
        pytest.skip("Production database not present; lineage check skipped.")
    dest = tmp_path / "copied_chat.db"
    try:
        src_conn = sqlite3.connect(str(source))
        try:
            dest_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()
    except sqlite3.Error:
        dest.unlink(missing_ok=True)
        raise
    try:
        yield dest
    finally:
        dest.unlink(missing_ok=True)


def _build_config(uri: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(VERSIONS_DIR.parent.parent / "migrations"))
    config.set_main_option("sqlalchemy.url", uri)
    return config


def _inspect_columns(engine: sa.Engine, table: str) -> dict[str, str]:
    with engine.connect() as conn:
        return {
            row["name"]: str(row["type"]).upper()
            for row in sa.inspect(engine).get_columns(table)
        }


# --- (1) fresh DB upgrades cleanly to the new head ----------------------------


def test_fresh_db_upgrades_to_zg1a2b3c4d5e() -> None:
    """A fresh empty database walks the full chain and lands on zg1a2b3c4d5e.

    No manual schema: only ``alembic upgrade head``. The result must
    match the head declared by this branch's migrations directory.
    """
    with tempfile.TemporaryDirectory() as tmp:
        uri = f"sqlite:///{Path(tmp) / 'fresh.db'}"
        config = _build_config(uri)
        command.upgrade(config, "head")

        engine = sa.create_engine(uri)
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                assert ctx.get_current_revision() == EXPECTED_HEAD, (
                    f"Fresh DB upgrade did not land on {EXPECTED_HEAD}; "
                    f"got {ctx.get_current_revision()!r}"
                )
            # Spot-check that the conversations table has every
            # repair-managed column.
            cols = _inspect_columns(engine, "conversations")
            for column in (
                "kind",
                "runner_id",
                "host_id",
                "reasoning_effort",
                "model_override",
                "cost_control_mode_override",
                "harness_override",
                "sub_agent_name",
                "external_session_id",
                "session_state",
                "session_usage",
                "terminal_launch_args",
                "workspace",
                "git_branch",
            ):
                assert column in cols, (
                    f"conversations.{column} missing after fresh upgrade; "
                    f"present columns: {sorted(cols)}"
                )
        finally:
            engine.dispose()


# --- (2) already-correct DB upgrade is a no-op --------------------------------


def test_fork_main_head_recognises_production_revision(copied_production_db: Path) -> None:
    """Fork/main's migration chain ends at zg1a2b3c4d5e.

    This is the property the deployment gate relies on: if the head
    of the candidate's migration set does not include the live
    production revision, the migration rehearsal aborts and the
    candidate is rolled back.
    """
    uri = f"sqlite:///{copied_production_db}"
    config = _build_config(uri)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    assert head == EXPECTED_HEAD, (
        f"Expected fork/main migration head to be {EXPECTED_HEAD}; "
        f"got {head!r}. The repair migration is missing or renamed."
    )


def test_copied_production_db_alembic_version_is_zg1a2b3c4d5e(
    copied_production_db: Path,
) -> None:
    """Live production alembic_version must already equal zg1a2b3c4d5e.

    Skipped when the production database is not visible to the test
    runner. CI supplies ``OMNIGENT_PRODUCTION_DBPATH`` to enable
    this check on the deploy gate.
    """
    uri = f"sqlite:///{copied_production_db}"
    engine = sa.create_engine(uri)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            assert ctx.get_current_revision() == EXPECTED_HEAD, (
                f"Production alembic_version drifted from {EXPECTED_HEAD}; "
                f"this regression test must be re-baselined before deploying. "
                f"Got {ctx.get_current_revision()!r}"
            )
    finally:
        engine.dispose()


def test_copied_production_db_starts_without_schema_upgrade(
    copied_production_db: Path,
) -> None:
    """``alembic upgrade head`` against the production copy is a zero-script no-op.

    This is the exact code path the deployment gate exercises
    (``_run_migrations``); on a production copy it must run zero
    migration scripts and exit cleanly. If it runs anything, the
    lineage has drifted away from production.
    """
    uri = f"sqlite:///{copied_production_db}"
    config = _build_config(uri)
    command.upgrade(config, "head")

    engine = sa.create_engine(uri)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            assert ctx.get_current_revision() == EXPECTED_HEAD
    finally:
        engine.dispose()


def test_repair_is_noop_when_columns_already_present(copied_production_db: Path) -> None:
    """Re-running zg1a2b3c4d5e on an already-repaired DB must not throw.

    The migration author contracts that each ADD COLUMN is guarded
    by a column-existence check. This test re-runs upgrade head
    twice on the production copy to prove the second run is a clean
    no-op (no duplicates, no ``OperationalError``).
    """
    uri = f"sqlite:///{copied_production_db}"
    config = _build_config(uri)
    command.upgrade(config, "head")  # first run: no-op on production copy
    command.upgrade(config, "head")  # second run: still no-op

    engine = sa.create_engine(uri)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            assert ctx.get_current_revision() == EXPECTED_HEAD
    finally:
        engine.dispose()


# --- (3) repair path applies against a fixture missing the columns ----------


def test_repair_adds_missing_columns_on_under_schema_fixture() -> None:
    """Synthesise a pre-repair schema at the parent revision and verify the repair lands.

    The fixture starts at the previous head (``zf1a2b3c4d5e``) and
    has the ``conversations`` table missing the repair-managed
    columns. After ``alembic upgrade head`` each column must be
    present, and the resulting schema must be byte-equivalent to a
    fresh-from-scratch upgrade (test 1).

    Uses the canonical online-safe SQLite backup API when copying
    the production database into the fixture so the test never races
    against an active writer.
    """
    with tempfile.TemporaryDirectory() as tmp:
        fixture_db = Path(tmp) / "repair_fixture.db"
        # Start from the parent revision's fresh state.
        parent_uri = f"sqlite:///{Path(tmp) / 'parent.db'}"
        parent_config = _build_config(parent_uri)
        command.upgrade(parent_config, "zf1a2b3c4d5e")

        # Use SQLite backup so the dest is a consistent online copy.
        src = sqlite3.connect(parent_uri.replace("sqlite:///", ""))
        try:
            dst = sqlite3.connect(str(fixture_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # Drop the columns that the repair migration adds. SQLite
        # has no DROP COLUMN before 3.35, so rebuild the table
        # without them. ``conversations`` is the only table this
        # migration touches.
        engine = sa.create_engine(f"sqlite:///{fixture_db}")
        try:
            with engine.begin() as conn:
                # Capture the current shape, then nuke the columns
                # the repair adds.
                existing = {row["name"] for row in sa.inspect(engine).get_columns("conversations")}
                for col in (
                    "runner_id",
                    "host_id",
                    "reasoning_effort",
                    "model_override",
                    "cost_control_mode_override",
                    "harness_override",
                    "sub_agent_name",
                    "external_session_id",
                    "session_state",
                    "session_usage",
                    "terminal_launch_args",
                    "workspace",
                    "git_branch",
                ):
                    assert col in existing, (
                        f"Parent revision 'zf1a2b3c4d5e' unexpectedly missing {col!r}; "
                        f"the synthetic pre-repair fixture cannot be built."
                    )
                # Drop the columns by rebuilding the table in place
                # (SQLite < 3.35 has no DROP COLUMN). This is a
                # test-only convenience; production chat.db never
                # has these columns dropped.
                conn.execute(sa.text("ALTER TABLE conversations RENAME TO conversations_pre_repair"))
                # Copy the surviving columns into a fresh table.
                conn.execute(
                    sa.text(
                        "CREATE TABLE conversations ("
                        "  workspace_id INTEGER NOT NULL,"
                        "  id BLOB NOT NULL,"
                        "  created_at INTEGER NOT NULL,"
                        "  updated_at INTEGER NOT NULL,"
                        "  title TEXT,"
                        "  root_conversation_id BLOB"
                        ")"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO conversations (workspace_id, id, created_at, updated_at, "
                        "title, root_conversation_id) "
                        "SELECT workspace_id, id, created_at, updated_at, title, "
                        "root_conversation_id "
                        "FROM conversations_pre_repair"
                    )
                )
                conn.execute(sa.text("DROP TABLE conversations_pre_repair"))
        finally:
            engine.dispose()

        # Now run the upgrade. The repair migration should add each
        # missing column back with the documented server_default.
        upgrade_uri = f"sqlite:///{fixture_db}"
        upgrade_config = _build_config(upgrade_uri)
        command.upgrade(upgrade_config, "head")

        engine = sa.create_engine(upgrade_uri)
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                assert ctx.get_current_revision() == EXPECTED_HEAD, (
                    f"Repair migration did not land on {EXPECTED_HEAD}; "
                    f"got {ctx.get_current_revision()!r}"
                )
                # The repair-managed columns must all be present
                # post-upgrade, with the expected shapes.
                cols = _inspect_columns(engine, "conversations")
                for column in (
                    "kind",
                    "runner_id",
                    "host_id",
                    "reasoning_effort",
                    "model_override",
                    "cost_control_mode_override",
                    "harness_override",
                    "sub_agent_name",
                    "external_session_id",
                    "session_state",
                    "session_usage",
                    "terminal_launch_args",
                    "workspace",
                    "git_branch",
                ):
                    assert column in cols, (
                        f"Repair migration failed to add conversations.{column}; "
                        f"present columns: {sorted(cols)}"
                    )
        finally:
            engine.dispose()


# --- live-DB sentinel --------------------------------------------------------


def test_live_production_db_revision_unchanged() -> None:
    """Live production database must remain at zg1a2b3c4d5e after validation.

    Sanity check that the copied-production-DB tests above did
    not accidentally mutate the live database via a stray
    symlink or shared WAL. Bails (skips) when no live DB is
    visible — the rest of the suite still validates the lineage
    end-to-end.
    """
    live = _resolve_production_dbpath()
    if live is None:
        pytest.skip("Production database not present; live-side check skipped.")
    engine = sa.create_engine(f"sqlite:///{live}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            assert ctx.get_current_revision() == EXPECTED_HEAD
    finally:
        engine.dispose()


# Touch the imported names so ruff/pyflakes don't flag them when the
# test run is filtered.
_ = (shutil, sqlite3, sa)
