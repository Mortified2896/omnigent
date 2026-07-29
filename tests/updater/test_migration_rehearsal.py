"""Migration rehearsal tests (issue #38 §5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omnigent.updater.migration_rehearsal import (
    backup_database,
    inspect_revisions,
    rehearse,
)


def _create_live_db(path: Path, *, revision: str | None) -> None:
    """Create a SQLite file with an ``alembic_version`` row.

    Mirrors how :func:`omnigent.db.utils._run_migrations` ends up
    on disk after a real upgrade.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        if revision is not None:
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (revision,))
        conn.commit()
    finally:
        conn.close()


def _make_candidate_tree(root: Path, head_revision: str) -> Path:
    """Build a minimal candidate tree the rehearsal helper can introspect."""
    candidate = root / "candidate"
    candidate.mkdir()
    versions = candidate / "omnigent" / "db" / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / f"{head_revision}_head.py").write_text(
        f"revision = '{head_revision}'\ndown_revision = None\n"
    )
    return candidate


def test_inspect_revisions_reads_live_revision(tmp_path: Path) -> None:
    """``inspect_revisions`` reads the live revision from the file."""
    db = tmp_path / "live.db"
    _create_live_db(db, revision="aaaa1234aaaa1234aaaa1234aaaa1234aaaa1234")
    candidate = _make_candidate_tree(tmp_path, "zzzz9999zzzz9999zzzz9999zzzz9999zzzz9999")
    live, cand = inspect_revisions(candidate_repo=candidate, db_url=f"sqlite:///{db}")
    assert live == "aaaa1234aaaa1234aaaa1234aaaa1234aaaa1234"
    assert cand == "zzzz9999zzzz9999zzzz9999zzzz9999zzzz9999"


def test_rehearse_records_inspected_not_required_when_head_matches(
    tmp_path: Path,
    state_root: Path,
) -> None:
    """When live and candidate revisions match, the rehearsal is recorded as
    ``required=False`` rather than silently skipped."""
    db = tmp_path / "live.db"
    candidate = _make_candidate_tree(tmp_path, "head_revision_head_revision_head_re")
    _create_live_db(db, revision="head_revision_head_revision_head_re")
    record = rehearse(
        request_id="AAAAAAAAAAAAAAAAAAAAAAAAAA",
        candidate_repo=candidate,
        db_url=f"sqlite:///{db}",
    )
    assert record.required is False
    assert record.live_revision == record.candidate_revision
    assert "rehearsal inspected" in record.notes[0]


def test_rehearse_skipped_when_live_db_missing(tmp_path: Path, state_root: Path) -> None:
    """A missing live DB does not error; the rehearsal records ``required=False``."""
    candidate = _make_candidate_tree(tmp_path, "zzzz9999zzzz9999zzzz9999zzzz9999zzzz9999")
    record = rehearse(
        request_id="BBBBBBBBBBBBBBBBBBBBBBBBBB",
        candidate_repo=candidate,
        db_url="sqlite:////nonexistent/does_not_exist.db",
    )
    assert record.required is False
    assert "no live database exists" in record.notes[0]


def test_backup_database_creates_consistent_copy(tmp_path: Path, state_root: Path) -> None:
    """``backup_database`` writes a SHA-256-tagged copy of the live DB."""
    db = tmp_path / "live.db"
    _create_live_db(db, revision="abc12345abc12345abc12345abc12345abc12345")
    target, sha = backup_database(
        request_id="CCCCCCCCCCCCCCCCCCCCCCCCCC",
        db_url=f"sqlite:///{db}",
    )
    assert target.is_file()
    assert len(sha) == 64
    # Verify the backup is still a valid SQLite DB with the
    # revision row intact.
    conn = sqlite3.connect(str(target))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row is not None
        assert row[0] == "abc12345abc12345abc12345abc12345abc12345"
    finally:
        conn.close()
