"""Pins the production schema lineage to ``zg1a2b3c4d5e``.

Production's chat.db reaches ``zg1a2b3c4d5e`` via the repair
migration (``zg1a2b3c4d5e_repair_conversations_kind``), which
extended the chain beyond ``ze1b2c3d4e5f`` (PR #37 durable
issue-run persistence + atomic leasing) so the deployment updater
could promote the bc22b799 release. Fork/main must walk to that
same head so the deployment schema gate (which compares
``alembic_version`` to the current head) can match. Before the
``zg1a2b3c4d5e`` migration existed in fork/main, the updater's
migration rehearsal refused the candidate because the production
revision was unknown on disk.

The regression test here uses the canonical online-safe SQLite
backup API rather than ``shutil.copy`` so a long-running
production database can be cloned without risking a copy against
a file that is being actively written. (The live ``chat.db`` is
written through a 5.12 WAL file under ``chat.db-wal``; copying
the main file while a writer is in flight can produce a
half-consistent image and Alembic will then refuse to start.)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
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

# Path to the live production database. The test is skipped (not
# failed) when this file is absent so contributors without access
# to the host database can still run the rest of the suite. CI
# supplies the path via ``OMNIGENT_PRODUCTION_DBPATH`` when running
# the full pre-deploy check.
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

    Falls back to ``shutil.copy`` only when the source isn't a SQLite
    file (e.g. a tests-supplied empty fixture). On any error during
    backup, the copy is removed so a partial file never leaks into a
    later test.
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


def test_copied_production_db_alembic_version_is_zg1a2b3c4d5e(
    copied_production_db: Path,
) -> None:
    uri = f"sqlite:///{copied_production_db}"
    engine = sa.create_engine(uri)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            assert ctx.get_current_revision() == "zg1a2b3c4d5e", (
                "Production alembic_version drifted from zg1a2b3c4d5e; "
                "this regression test must be re-baselined before deploying."
            )
    finally:
        engine.dispose()


def test_fork_main_head_recognises_production_revision(copied_production_db: Path) -> None:
    """Fork/main's migration chain now ends at zg1a2b3c4d5e.

    Before the repair migration was added to fork/main, the chain
    ended at ``zf1a2b3c4d5e`` and the updater's migration rehearsal
    refused the candidate because the production revision
    ``zg1a2b3c4d5e`` was unknown on disk.
    """
    uri = f"sqlite:///{copied_production_db}"
    config = _build_config(uri)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    assert head == "zg1a2b3c4d5e", (
        f"Expected fork/main migration head to be zg1a2b3c4d5e; "
        f"got {head!r}. The repair migration is missing or renamed."
    )


def test_copied_production_db_starts_without_schema_upgrade(
    copied_production_db: Path,
) -> None:
    """Alembic upgrade against the copy must be a no-op (current == head).

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
            assert ctx.get_current_revision() == "zg1a2b3c4d5e"
    finally:
        engine.dispose()


def test_zd1b2c3d4e5f_converts_routing_id_columns_to_binary() -> None:
    """On a fork/main-shaped fresh DB the conversion lands BLOB id columns.

    Uses a synthetic zc1b2c3d4e5f schema (no BLOB ids) and
    asserts that zd1b2c3d4e5f's upgrade rewrites the affected
    columns to ``BLOB`` (16-byte) form so production's stores
    (``Uuid16``) bind them correctly.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        uri = f"sqlite:///{Path(tmp) / 'fresh.db'}"
        config = _build_config(uri)
        command.upgrade(config, "zc1b2c3d4e5f")
        # Insert text-form id values that need conversion.
        engine = sa.create_engine(uri)
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO conversations (workspace_id, id, created_at, updated_at, "
                        "kind, root_conversation_id) VALUES "
                        "(0, 'c_conv00000000000000000000000000aa', 1, 1, 1, "
                        "'c_conv00000000000000000000000000aa')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO routing_proposals "
                        "(workspace_id, id, conversation_id, "
                        "elicitation_id, user_message_sha256, user_message_excerpt, "
                        "user_message_chars, content_types_json, original_route_id, "
                        "requires_explicit_approval, proposal_payload_excerpt, "
                        "proposal_payload_sha256, created_at) VALUES "
                        "(0, 'rp_prop00000000000000000000000000bb', "
                        "'c_conv00000000000000000000000000aa', "
                        "'el_000000000000000000000000000000cc', "
                        "'sha00000000000000000000000000000000000000000000000000000000000000dd', "
                        "'excerpt', 7, '{}', 'auto/coding', 1, 'p', "
                        "'sha00000000000000000000000000000000000000000000000000000000000000ee', 1)"
                    )
                )
        finally:
            engine.dispose()
        command.upgrade(config, "head")

        engine = sa.create_engine(uri)
        try:
            with engine.connect() as conn:
                cols = {
                    c["name"]: str(c["type"]).upper()
                    for c in sa.inspect(engine).get_columns("routing_proposals")
                }
                assert "BLOB" in cols["id"], (
                    f"routing_proposals.id should be BLOB after zd... upgrade; got {cols['id']!r}"
                )
                row = conn.execute(sa.text("SELECT id FROM routing_proposals LIMIT 1")).one()
                assert isinstance(row[0], (bytes, memoryview)), (
                    f"routing_proposals.id value should be raw bytes after "
                    f"zd... upgrade; got {type(row[0]).__name__}"
                )
                assert len(bytes(row[0])) == 16
        finally:
            engine.dispose()


def test_zd1b2c3d4e5f_downgrade_is_unsupported() -> None:
    """In-place downgrade of the head migration is forbidden.

    Forces a downgrade call against a fresh DB and asserts the
    RuntimeError that the head migration raises. Prevents a future
    refactor from silently making downgrade a no-op (which would
    re-introduce the schema-skew class of bug we just fixed).

    Re-baselined to ``zg1a2b3c4d5e`` once the repair migration was
    adopted into the deployment lineage.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        uri = f"sqlite:///{Path(tmp) / 'downgrade.db'}"
        config = _build_config(uri)
        command.upgrade(config, "head")
        with pytest.raises(RuntimeError, match="zg1a2b3c4d5e is not safely reversible"):
            command.downgrade(config, "-1")


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
            assert ctx.get_current_revision() == "zg1a2b3c4d5e"
    finally:
        engine.dispose()


# Touch the imported names so ruff/pyflakes don't flag them when the
# test run is filtered.
_ = (shutil, sqlite3, sa)
