"""Conservative metadata manifests and bounded SQLite backup/restore checks."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from managed_budget import metadata_cleanup_decision
from managed_storage import StoragePaused, exclusive, measure


def evidence(row, references=None):
    """Idle reconciliation is not an observed completion or reference clearance."""
    references = references or {}
    return {
        "completed": row["end_hook"] == "Stop" and row["end_complete"] == 1,
        "completed_at": row["end_time"],
        "frozen": row["frozen"] != 0,
        "in_flight": references.get("in_flight"),
        "captures_pruned": bool(row["pruned_at"] and row["full_trace_pruned_at"]),
        "needed_for_retained_evidence": references.get("needed_for_retained_evidence"),
        "needed_for_rollback": references.get("needed_for_rollback"),
    }


def cleanup_manifest(conn, policy, references=lambda _row: {}, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    counts = Counter()
    eligible = []
    total = frozen = incomplete = 0
    for row in conn.execute("SELECT * FROM turns ORDER BY session_id,turn_id"):
        total += 1
        frozen += row["frozen"] != 0
        incomplete += row["end_time"] is None
        decision = metadata_cleanup_decision(
            evidence(row, references(row)), policy=policy, now=now
        )
        counts[decision["reason"]] += 1
        if decision["eligible"]:
            # Batches and the returned manifest are bounded; later runs can continue.
            if len(eligible) < 128:
                eligible.append((row["session_id"], row["turn_id"]))
    digest = hashlib.sha256(json.dumps(eligible, separators=(",", ":")).encode()).hexdigest()
    return {
        "dry_run": True,
        "examined": total,
        "eligible_count": counts["expired_completed_unreferenced"],
        "batch_count": len(eligible),
        "batch_sha256": digest,
        "frozen": frozen,
        "without_end_time": incomplete,
        "reasons": dict(counts),
        "applied": 0,
    }, eligible


def verified_backup(conn, root, allocation, deadline_seconds=10):
    """One retained backup slot, two bounded temporary files; never copy live WAL."""
    root = Path(root)
    if not root.is_dir() or root.resolve() != root:
        raise StoragePaused("backup_root_unknown")
    final = root / "metadata-before-cleanup.sqlite3"
    temporary = root / "metadata-backup.tmp"
    restored = root / "metadata-restore.tmp"
    with exclusive(root / ".backup.lock"):
        if any(p.exists() for p in (final, temporary, restored)):
            raise StoragePaused("backup_slot_requires_review")
        inventory = measure([{"path": str(root), "component": "telemetry_backups"}])
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        upper = pages * page_size
        used = inventory["components"]["telemetry_backups"]["bytes"]
        if not inventory["complete"] or used + 2 * upper + 262144 > allocation:
            raise StoragePaused("backup_reserve_unavailable")
        start = time.monotonic()

        def progress(_status, _remaining, total):
            if total * page_size > upper or time.monotonic() - start > deadline_seconds:
                raise StoragePaused("backup_changed_or_timed_out")

        try:
            with closing(sqlite3.connect(temporary)) as target:
                conn.backup(target, pages=64, progress=progress, sleep=0.01)
                target.execute("PRAGMA journal_mode=DELETE")
            # Restore from the actual backup through the same supported SQLite API.
            with (
                closing(sqlite3.connect(temporary)) as source,
                closing(sqlite3.connect(restored)) as target,
            ):
                source.backup(target, pages=64, progress=progress, sleep=0.01)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StoragePaused("restore_integrity_failed")
                if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise StoragePaused("restore_reference_check_failed")
                rows = target.execute("SELECT count(*) FROM turns").fetchone()[0]
                original = source.execute("SELECT count(*) FROM turns").fetchone()[0]
                if rows != original:
                    raise StoragePaused("restore_rows_mismatch")
            if temporary.read_bytes() != restored.read_bytes():
                # SQLite backup may change header counters; compare logical contents.
                with (
                    closing(sqlite3.connect(temporary)) as a,
                    closing(sqlite3.connect(restored)) as b,
                ):
                    if list(a.iterdump()) != list(b.iterdump()):
                        raise StoragePaused("restore_contents_mismatch")
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
            temporary.chmod(0o600)
            temporary.replace(final)
            return {
                "path": str(final),
                "backup_sha256": digest,
                "restored_rows": rows,
                "integrity": "ok",
                "foreign_key_errors": 0,
                "backup_bytes": final.stat().st_size,
                "restore_test": "isolated; live database untouched",
            }
        finally:
            temporary.unlink(missing_ok=True)
            restored.unlink(missing_ok=True)


def apply_cleanup(conn, policy, batch, references, backup, now=None):
    """Recheck every predicate in one bounded transaction; keep no tombstone rows.

    The caller must hold the existing writer lock and supply reference evidence
    coordinated with retained archive/active-work/rollback owners. Unknown stays.
    """
    if not isinstance(backup, dict) or len(batch) > 128:
        raise StoragePaused("cleanup_backup_or_batch_unverified")
    path = Path(backup["path"])
    if (
        path.is_symlink()
        or hashlib.sha256(path.read_bytes()).hexdigest() != backup["backup_sha256"]
    ):
        raise StoragePaused("cleanup_backup_changed")
    restored = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    restored.row_factory = sqlite3.Row
    now = now or dt.datetime.now(dt.timezone.utc)
    applied = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for session, turn in batch:
            row = conn.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_id=?", (session, turn)
            ).fetchone()
            previous = restored.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_id=?", (session, turn)
            ).fetchone()
            if row is None or previous is None or tuple(row) != tuple(previous):
                continue
            decision = metadata_cleanup_decision(
                evidence(row, references(row)), policy=policy, now=now
            )
            if decision["eligible"]:
                conn.execute("DELETE FROM turns WHERE session_id=? AND turn_id=?", (session, turn))
                applied += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        restored.close()
    return {"applied": applied, "preserved_after_recheck": len(batch) - applied}
