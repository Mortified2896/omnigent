#!/usr/bin/env python3
"""Read-only data-loss and migration-graph checks for the 0.16 reconciliation.

Run against consistent SQLite backup copies, separately for O1 and O2. This is
not the deployment controller, a migration runner, or deployment acceptance.
Only aggregate counts are emitted; never preference, snapshot, or feedback text.
"""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

LIMIT = 65_535
MAX_SCAN_BYTES = 16 * 1024 * 1024
CUSTOM_REVISIONS = ("f8a9b0c1d2e3", "b4d8e2f6a9c1", "c91f6a2d7e40")
TARGET_REVISIONS = (*CUSTOM_REVISIONS, "kk1a2b3c4d5e", "ll1a2b3c4d5e")
COUNT_TABLES = (
    "conversations",
    "conversation_items",
    "model_advisor_records",
    "response_feedback",
    "generated_response_audio",
    "scheduled_tasks",
)


class AuditError(ValueError):
    """The supplied evidence cannot be audited safely."""


def stored_bytes(value: str | bytes | memoryview) -> bytes:
    """Measure legacy SQLite TEXT in UTF-8 bytes, including embedded NULs."""
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytes, memoryview)):
        return bytes(value)
    raise AuditError("unsupported stored value type")


def snapshot_size(value: str | bytes | memoryview) -> int:
    """Mirror the pinned ll1a2b3c4d5e codec; bound untrusted decompression."""
    data = stored_bytes(value)
    if len(data) > MAX_SCAN_BYTES:
        raise AuditError("snapshot exceeds bounded scan budget")
    # The migration treats TEXT as plaintext, even when it begins with NUL.
    if not isinstance(value, str):
        if data.startswith(b"\x00\x01"):
            try:
                import zstandard
            except ImportError as exc:
                raise AuditError("zstandard is required to inspect compressed snapshots") from exc
            try:
                frame = data[2:]
                if zstandard.frame_content_size(frame) > MAX_SCAN_BYTES:
                    raise AuditError("decompressed snapshot exceeds bounded scan budget")
                data = zstandard.ZstdDecompressor().decompress(
                    frame, max_output_size=MAX_SCAN_BYTES
                )
            except zstandard.ZstdError as exc:
                raise AuditError("invalid compressed snapshot") from exc
            if len(data) > MAX_SCAN_BYTES:
                raise AuditError("decompressed snapshot exceeds bounded scan budget")
        elif data.startswith(b"\x00\x00"):
            data = data[2:]
        data.decode("utf-8")  # The migration also rejects invalid UTF-8.
    if len(data) < 64:
        return 2 + len(data)
    try:
        import zstandard
    except ImportError as exc:
        raise AuditError("zstandard is required to measure compressed snapshots") from exc
    return 2 + len(zstandard.ZstdCompressor(level=19).compress(data))


def _columns(db: sqlite3.Connection, table: str) -> dict[str, str]:
    # Identifiers here and in audit_database come only from fixed source constants.
    return {
        str(row[1]): str(row[2]).upper() for row in db.execute(f'PRAGMA table_info("{table}")')
    }


def audit_database(path: Path) -> dict[str, Any]:
    """Inspect a supplied copy without creating, rewriting, or repairing it."""
    path = path.resolve(strict=True)
    if not path.is_file():
        raise AuditError("database must be an existing regular file")
    result: dict[str, Any] = {"blockers": [], "row_counts": {}, "size_checks": {}}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise AuditError("SQLite integrity check failed")
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "alembic_version" not in tables:
            raise AuditError("database has no Alembic revision evidence")
        revisions = [str(row[0]) for row in db.execute("SELECT version_num FROM alembic_version")]
        if not revisions or any(not rev.isalnum() or len(rev) > 64 for rev in revisions):
            raise AuditError("invalid or missing Alembic revision evidence")
        result["revisions"] = sorted(revisions)
        for table in COUNT_TABLES:
            result["row_counts"][table] = (
                db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if table in tables
                else None
            )
        # The users column is the intermediate pre-preferences-table layout.
        # A missing column is explicitly reported, never interpreted as zero rows.
        for table, column, kind in (
            ("preferences", "value", "stored"),
            ("users", "preferences", "stored"),
            ("omnigent_conversation_metadata", "inference_snapshot", "snapshot"),
        ):
            key = f"{table}.{column}"
            columns = _columns(db, table)
            if column not in columns:
                result["size_checks"][key] = {"present": False}
                continue
            scan: dict[str, Any] = {
                "present": True,
                "non_null": 0,
                "oversized": 0,
                "unreadable": 0,
            }
            # A completed snapshot conversion already stores framed bytes. Do not
            # recompress those: ll1a2b3c4d5e skips an already-binary column.
            binary_snapshot = kind == "snapshot" and columns[column] == "BLOB"
            mode = "stored" if kind == "stored" or binary_snapshot else "recompressed"
            scan["measurement"] = mode
            rows = db.execute(
                f'SELECT typeof("{column}"), length(CAST("{column}" AS BLOB)), '
                f'CASE WHEN length(CAST("{column}" AS BLOB)) <= ? THEN "{column}" END '
                f'FROM "{table}" WHERE "{column}" IS NOT NULL',
                (MAX_SCAN_BYTES,),
            )
            for value_type, raw_size, value in rows:
                scan["non_null"] += 1
                if value_type not in ("text", "blob"):
                    scan["unreadable"] += 1
                    continue
                if mode == "stored":
                    scan["oversized"] += int(raw_size > LIMIT)
                    continue
                if value is None:
                    scan["unreadable"] += 1
                    continue
                try:
                    scan["oversized"] += int(snapshot_size(value) > LIMIT)
                except (AuditError, UnicodeError):
                    scan["unreadable"] += 1
            result["size_checks"][key] = scan
            if scan["oversized"] or scan["unreadable"]:
                result["blockers"].append(
                    f"{key}: preserve affected values and review before migration"
                )
        # Counts alone are not a data-preservation proof. Compare row identities and
        # payload hashes privately during the real migration rehearsal as well.
        db.rollback()
    return result


def _assignments(path: Path) -> dict[str, Any]:
    """Parse Alembic metadata without importing or executing migration code."""
    result: dict[str, Any] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        target = None
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        if target in {"revision", "down_revision", "depends_on"}:
            if target in result or value is None:
                raise AuditError("ambiguous migration metadata")
            try:
                result[target] = ast.literal_eval(value)
            except (ValueError, TypeError) as exc:
                raise AuditError(
                    "nonliteral migration metadata requires manual inspection"
                ) from exc
    if "revision" not in result or "down_revision" not in result:
        raise AuditError("missing migration metadata")
    return result


def _parents(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) and v for v in value):
        if len(set(value)) == len(value):
            return tuple(value)
    raise AuditError("invalid migration parents")


def audit_migrations(directory: Path, reference: Path | None = None) -> dict[str, Any]:
    """Require one joined history and preserve the three deployed custom scripts."""
    graph: dict[str, tuple[str, ...]] = {}
    files: dict[str, Path] = {}
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        meta = _assignments(path)
        revision = meta["revision"]
        if not isinstance(revision, str) or not revision or revision in graph:
            raise AuditError("invalid or duplicate migration revision")
        if meta.get("depends_on") is not None:
            raise AuditError("dependency-bearing migrations require Alembic inspection")
        graph[revision] = _parents(meta["down_revision"])
        files[revision] = path
    if not graph:
        raise AuditError("no migration scripts found")
    referenced = {parent for parents in graph.values() for parent in parents}
    if referenced - graph.keys():
        raise AuditError("migration history contains missing parents")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(revision: str) -> None:
        if revision in visiting:
            raise AuditError("migration history contains a cycle")
        if revision in visited:
            return
        visiting.add(revision)
        for parent in graph[revision]:
            visit(parent)
        visiting.remove(revision)
        visited.add(revision)

    for revision in graph:
        visit(revision)
    heads = sorted(graph.keys() - referenced)
    blockers = []
    missing = sorted(set(TARGET_REVISIONS) - graph.keys())
    if missing:
        blockers.append("required fork/upstream revisions missing: " + ", ".join(missing))
    if len(heads) != 1:
        blockers.append("join the fork and upstream heads with a NEW Alembic merge revision")
    if reference is None:
        blockers.append(
            "historical script preservation not checked: supply --reference-migrations"
        )
    else:
        for revision in CUSTOM_REVISIONS:
            originals = [path for path in reference.glob(f"{revision}_*.py") if path.is_file()]
            if len(originals) != 1 or revision not in files:
                blockers.append(f"cannot verify original custom migration {revision}")
            elif originals[0].read_bytes() != files[revision].read_bytes():
                blockers.append(f"deployed custom migration was rewritten: {revision}")
    return {"heads": heads, "revision_count": len(graph), "blockers": blockers}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="append", default=[], metavar="O1=/backup/db.sqlite")
    parser.add_argument("--migrations", type=Path)
    parser.add_argument("--reference-migrations", type=Path)
    args = parser.parse_args(argv)
    if not args.database and args.migrations is None:
        parser.error("supply --database and/or --migrations")
    result: dict[str, Any] = {"deployment_accepted": False, "databases": {}}
    seen: set[tuple[int, int]] = set()
    try:
        for item in args.database:
            label, separator, raw_path = item.partition("=")
            if not separator or label not in {"O1", "O2"} or label in result["databases"]:
                raise AuditError("database arguments require unique O1/O2 labels")
            path = Path(raw_path)
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                raise AuditError("O1 and O2 must not use the same database copy")
            seen.add(identity)
            result["databases"][label] = audit_database(path)
        if args.migrations is not None:
            result["migrations"] = audit_migrations(args.migrations, args.reference_migrations)
    except (AuditError, OSError, sqlite3.Error, SyntaxError, ImportError) as exc:
        # Do not serialize database content or arbitrary exception text into logs.
        print(
            json.dumps(
                {
                    "deployment_accepted": False,
                    "error": type(exc).__name__,
                    "detail": (
                        str(exc) if isinstance(exc, AuditError) else "audit could not be completed"
                    ),
                }
            )
        )
        return 2
    checks = list(result["databases"].values())
    if "migrations" in result:
        checks.append(result["migrations"])
    result["checks_passed"] = all(not check["blockers"] for check in checks)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["checks_passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
