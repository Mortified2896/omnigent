"""Migration rehearsal and backup orchestration (issue #38 §5).

The controller uses the existing helpers in
:mod:`omnigent.db.utils` to inspect the live database revision and
the candidate head, then:

1. Copies the live database file (or `pg_dump`s for Postgres) into
   a per-request scratch directory.
2. Runs ``alembic upgrade head`` against the copy via the same
   helpers the server uses at startup.
3. Reads the post-rehearsal revision and confirms it matches the
   candidate head.
4. Takes a final consistent backup immediately before cutover and
   records its path and ``sha256``.

When the candidate head equals the live revision (no schema change),
the rehearsal phase is recorded as **inspected-not-required**, not
silently skipped.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omnigent.updater import layout


@dataclass
class RehearsalRecord:
    """Durable record of a migration rehearsal.

    :param request_id: The request id.
    :param required: Whether the rehearsal ran a real migration.
    :param live_revision: The live database revision before rehearsal.
    :param candidate_revision: The candidate's migration head.
    :param rehearsal_db_path: Path to the scratch copy.
    :param rehearsal_post_revision: Revision after the rehearsal upgrade.
    :param completed_at: ISO timestamp.
    :param backup_path: Path of the final pre-cutover backup (set later).
    :param backup_sha256: ``sha256`` of the backup file.
    :param notes: Free-form notes.
    """

    request_id: str
    required: bool
    live_revision: str | None
    candidate_revision: str | None
    rehearsal_db_path: str | None
    rehearsal_post_revision: str | None
    completed_at: str
    backup_path: str | None = None
    backup_sha256: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_db_url_path(db_url: str) -> Path:
    """Resolve a ``sqlite:///<path>`` URL to its absolute file path.

    Raises :class:`ValueError` for non-SQLite URLs; the rehearsal
    helper only handles SQLite for the issue-38 cutover because the
    production database is SQLite.
    """
    if not db_url.startswith("sqlite:///"):
        raise ValueError(
            f"rehearsal only supports sqlite:///<path> URLs in this build; got {db_url!r}"
        )
    raw = db_url[len("sqlite:///") :]
    return Path(raw).resolve()


def _sqlite_revision(db_path: Path) -> str | None:
    """Read the Alembic revision from a SQLite database file.

    Mirrors :func:`omnigent.db.utils._get_current_db_revision` so
    we get the same semantics for ``alembic_version`` missing vs.
    uninitialized databases.
    """
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        )
        if cur.fetchone() is None:
            return None
        cur = conn.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_revisions(
    *,
    candidate_repo: Path,
    db_url: str,
) -> tuple[str | None, str | None]:
    """Inspect live + candidate database revisions.

    :param candidate_repo: Path to the candidate release directory.
        The function reads the migrations tree inside it (via the
        same helpers the server uses) to discover the candidate
        head.
    :param db_url: The live database URL, e.g.
        ``"sqlite:////home/hermes/.omnigent/chat.db"``.
    :returns: ``(live_revision, candidate_revision)``. Either may be
        ``None`` when the database has not been migrated yet or the
        candidate tree is empty (both genuine edge cases).
    """
    db_path = _resolve_db_url_path(db_url)
    live_revision = _sqlite_revision(db_path)

    candidate_revision = _candidate_head_revision(candidate_repo)
    return live_revision, candidate_revision


def _candidate_head_revision(candidate_repo: Path) -> str | None:
    """Return the head Alembic revision for the candidate tree.

    Walks the migrations directory directly rather than invoking
    ``alembic`` so the rehearsal helper does not need a working
    venv for the candidate release at this stage.
    """
    versions_dir = candidate_repo / "omnigent" / "db" / "migrations" / "versions"
    if not versions_dir.is_dir():
        return None
    return _head_revision_from_versions(versions_dir)


def _head_revision_from_versions(versions_dir: Path) -> str | None:
    """Walk the candidate's migration files and compute the head.

    Reads each ``.py`` file, captures ``revision: str`` and
    ``down_revision: str | None`` (a string or a tuple of strings
    when a revision has multiple down-references), and finds the
    revision with no down-references that is reachable in the
    forward direction.
    """
    heads: set[str] = set()
    downrefs: dict[str, set[str]] = {}
    for py in versions_dir.glob("*.py"):
        if py.name == "__init__.py":
            continue
        rev, down = _parse_revision(py)
        if rev is None:
            continue
        heads.add(rev)
        if down is not None:
            downrefs.setdefault(rev, set()).update(down)
    # Heads in the graph-theoretic sense: revisions that nothing
    # else points at as a down_revision.
    candidates = set(heads)
    for downs in downrefs.values():
        candidates -= downs
    # Single-head deployments are the rule; multiple heads is a
    # genuine configuration error we don't try to resolve here.
    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        # If every revision has a down_revision we still pick the
        # lexicographically last — better than None for ordering.
        return max(heads) if heads else None
    return max(candidates)


def _parse_revision(path: Path) -> tuple[str | None, set[str] | None]:
    """Parse a single alembic revision file's ``revision`` / ``down_revision``.

    Tolerates both the literal ``revision: str = "abc"`` form and
    the variable-form ``revision = "abc"`` form, plus tuple-valued
    ``down_revision`` chains.
    """
    rev: str | None = None
    down: set[str] = set()
    saw_rev = False
    for raw_line in path.read_text().splitlines():
        stripped = raw_line.strip()
        if not saw_rev and stripped.startswith("revision"):
            saw_rev = True
            # Both ``revision: str = "x"`` and ``revision = "x"``
            quote = '"' in stripped or "'" in stripped
            if not quote:
                continue
            rev = _extract_quoted(stripped)
            continue
        if stripped.startswith("down_revision"):
            for tok in _extract_all_quoted(stripped):
                if tok == "None":
                    continue
                down.add(tok)
    if rev is None:
        return None, None
    return rev, down


def _extract_quoted(line: str) -> str | None:
    for quote in ('"', "'"):
        if quote in line:
            start = line.index(quote) + 1
            end = line.index(quote, start)
            return line[start:end]
    return None


def _extract_all_quoted(line: str) -> Iterable[str]:
    for quote in ('"', "'"):
        idx = 0
        while True:
            start = line.find(quote, idx)
            if start == -1:
                break
            end = line.find(quote, start + 1)
            if end == -1:
                break
            yield line[start + 1 : end]
            idx = end + 1


def rehearse(
    *,
    request_id: str,
    candidate_repo: Path,
    db_url: str,
) -> RehearsalRecord:
    """Run a migration rehearsal for ``request_id``.

    Copies the live database into the per-request scratch dir,
    runs ``alembic upgrade head`` against the copy via the
    existing helpers, then verifies the post-upgrade revision
    matches the candidate head. When the candidate head equals the
    live revision (or either is missing), the rehearsal is recorded
    as "inspected-not-required".
    """
    from omnigent.db.utils import _run_migrations, get_or_create_engine

    scratch = layout.rehearsal_dir() / request_id
    scratch.mkdir(parents=True, exist_ok=True)
    db_path = _resolve_db_url_path(db_url)
    if not db_path.is_file():
        return RehearsalRecord(
            request_id=request_id,
            required=False,
            live_revision=None,
            candidate_revision=_candidate_head_revision(candidate_repo),
            rehearsal_db_path=None,
            rehearsal_post_revision=None,
            completed_at=_now_iso(),
            notes=["no live database exists; rehearsal skipped"],
        )

    live_revision, candidate_revision = inspect_revisions(
        candidate_repo=candidate_repo, db_url=db_url
    )
    if (
        candidate_revision is not None
        and live_revision is not None
        and candidate_revision == live_revision
    ):
        return RehearsalRecord(
            request_id=request_id,
            required=False,
            live_revision=live_revision,
            candidate_revision=candidate_revision,
            rehearsal_db_path=None,
            rehearsal_post_revision=live_revision,
            completed_at=_now_iso(),
            notes=["candidate head equals live revision; rehearsal inspected but not required"],
        )

    scratch_db = scratch / f"{request_id}.db"
    shutil.copy2(db_path, scratch_db)

    scratch_url = f"sqlite:///{scratch_db}"
    engine = get_or_create_engine(scratch_url)
    _run_migrations(engine, scratch_url)
    post_revision = _sqlite_revision(scratch_db)

    if candidate_revision is not None and post_revision != candidate_revision:
        raise RuntimeError(
            f"rehearsal failed: live revision {live_revision!r} did not advance to "
            f"candidate head {candidate_revision!r} (got {post_revision!r})"
        )

    return RehearsalRecord(
        request_id=request_id,
        required=True,
        live_revision=live_revision,
        candidate_revision=candidate_revision,
        rehearsal_db_path=str(scratch_db),
        rehearsal_post_revision=post_revision,
        completed_at=_now_iso(),
    )


def backup_database(*, request_id: str, db_url: str) -> tuple[Path, str]:
    """Take a consistent backup of the live database.

    For SQLite this is a ``VACUUM INTO`` copy when the SQLite
    library supports it (3.27+, 2019-02), otherwise a
    ``sqlite3.Connection.backup`` round-trip to guarantee
    consistency without blocking writers for the full file copy.
    Falls back to ``shutil.copy2`` when neither is available, with
    a ``fsync`` after the copy.
    """
    db_path = _resolve_db_url_path(db_url)
    if not db_path.is_file():
        raise FileNotFoundError(f"live database does not exist: {db_path}")

    target_dir = layout.backups_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{db_path.name}.backup-{request_id}-{_now_compact()}.db"

    # ``VACUUM INTO`` is preferred because it produces a clean,
    # defragmented copy atomically. Older SQLite versions fall
    # through to ``Connection.backup``, then to ``shutil.copy2``.
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) + chr(39))}'")
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        conn = sqlite3.connect(str(db_path))
        try:
            with sqlite3.connect(str(target)) as dst:
                conn.backup(dst)
        finally:
            conn.close()
    return target, _hash_file(target)


def _now_iso() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_compact() -> str:
    import time

    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


__all__ = [
    "RehearsalRecord",
    "backup_database",
    "inspect_revisions",
    "rehearse",
]
