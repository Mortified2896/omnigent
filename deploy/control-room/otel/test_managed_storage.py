"""Small real-file/process/SQLite fixtures; never use the installed telemetry roots."""

import contextlib
import datetime as dt
import io
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_otel_decisions as provenance
from managed_budget import load_policy
from managed_metadata import apply_cleanup, cleanup_manifest, verified_backup
from managed_sqlite import BoundedConnection
from managed_storage import BoundedFiles, BoundedLog, StoragePaused, exclusive, measure


def compete(root, name, start, results):
    start.wait()
    try:
        BoundedFiles(Path(root), 125000, 50000).write(Path(root) / name, b"x" * 50000)
        results.put("written")
    except StoragePaused:
        results.put("paused")


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "captures"
        self.root.mkdir()

    def scan(self):
        return measure([{"path": str(self.root), "component": "provenance_captures"}])

    def test_all_files_and_protected_bytes_count(self):
        (self.root / "frozen").write_bytes(b"x" * 200)
        scan = self.scan()
        self.assertTrue(scan["complete"])
        used = scan["components"]["provenance_captures"]
        self.assertGreaterEqual(used["bytes"], 200)
        self.assertEqual(used["protected_bytes"], used["bytes"])
        self.assertIsNone(scan["reserved_bytes"])
        self.assertFalse(scan["enforcement_verified"])

    def test_internal_hardlinks_count_once(self):
        (self.root / "a").write_bytes(b"x" * 100)
        os.link(self.root / "a", self.root / "b")
        scan = self.scan()
        self.assertTrue(scan["complete"])
        self.assertLess(scan["components"]["provenance_captures"]["logical_bytes"], 1000)

    def test_external_hardlinks_remain_unknown(self):
        (self.root / "a").write_bytes(b"x")
        os.link(self.root / "a", self.root.parent / "external")
        self.assertIn("external_hardlink", self.scan()["errors"])

    def test_missing_root_and_symlink_fail_closed(self):
        result = measure([{"path": str(self.root / "missing"), "component": "other_managed"}])
        self.assertFalse(result["complete"])
        (self.root / "link").symlink_to(self.root.parent)
        self.assertFalse(self.scan()["complete"])
        with self.assertRaises(StoragePaused):
            BoundedFiles(self.root, 100000).write(self.root / "new", b"x")

    def test_overlapping_roots_rejected(self):
        roots = [
            {"path": str(p), "component": "other_managed"} for p in (self.root, self.root.parent)
        ]
        self.assertIn("overlapping_roots", measure(roots)["errors"])

    def test_oversized_write_creates_nothing(self):
        with self.assertRaises(StoragePaused):
            BoundedFiles(self.root, 100000, 2).write(self.root / "a", b"xxx")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_protected_only_fullness_refuses_growth(self):
        (self.root / "frozen").write_bytes(b"x" * 1000)
        with self.assertRaises(StoragePaused):
            BoundedFiles(self.root, 1000).write(self.root / "a", b"x")
        self.assertEqual((self.root / "frozen").stat().st_size, 1000)

    def test_two_processes_cannot_spend_same_headroom(self):
        ctx = multiprocessing.get_context("spawn")
        start, results = ctx.Event(), ctx.Queue()
        processes = [
            ctx.Process(target=compete, args=(str(self.root), str(i), start, results))
            for i in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted(outcomes), ["paused", "written"])
        self.assertLess(self.scan()["components"]["provenance_captures"]["bytes"], 125000)

    def test_failed_replace_preserves_original_and_cleans_scratch(self):
        target = self.root / "a"
        target.write_bytes(b"old")
        with (
            patch("managed_storage.os.replace", side_effect=OSError("interrupted")),
            self.assertRaises(OSError),
        ):
            BoundedFiles(self.root, 100000).write(target, b"new")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_log_rollover_closes_descriptors_and_bounds_segments(self):
        log = BoundedLog(self.root / "managed.log", 100, 2, 80)
        for i in range(20):
            log.write(str(i) + "x" * 60 + "\n")
        files = list(self.root.glob("managed.log*"))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(p.stat().st_size <= 100 for p in files))
        self.assertTrue((self.root / "managed.log").read_text().startswith("19"))

    def test_oversized_log_record_is_bounded(self):
        log = BoundedLog(self.root / "managed.log", 100, 2, 80)
        log.write("x" * 10000)
        self.assertLessEqual((self.root / "managed.log").stat().st_size, 100)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "provenance.sqlite3"
        self.conn = sqlite3.connect(self.path, factory=BoundedConnection)
        self.conn.configure(self.path, 2 * 1024**2, 65536)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE data(value BLOB)")
        self.conn.execute("INSERT INTO data VALUES(?)", (b"a",))
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def test_oversized_record_refused_before_transaction(self):
        with self.assertRaises(StoragePaused):
            self.conn.execute("INSERT INTO data VALUES(?)", (b"x" * 65537,))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM data").fetchone()[0], 1)

    def test_pinned_reader_applies_backpressure(self):
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM data").fetchall()
        with self.assertRaises(StoragePaused):
            self.conn.execute("INSERT INTO data VALUES(?)", (b"x",))
        reader.rollback()
        self.conn.execute("INSERT INTO data VALUES(?)", (b"x",))
        self.conn.commit()

    def test_main_and_wal_remain_inside_component_under_fullness(self):
        with self.assertRaises(sqlite3.OperationalError):
            for _ in range(100):
                self.conn.execute("INSERT INTO data VALUES(?)", (b"x" * 32000,))
                self.conn.commit()
        self.conn.rollback()
        total = sum(p.stat().st_size for p in self.root.glob("provenance.sqlite3*"))
        self.assertLess(total, 2 * 1024**2)
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_interrupted_transaction_recovers(self):
        self.conn.execute("INSERT INTO data VALUES(?)", (b"uncommitted",))
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM data").fetchone()[0], 1)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.conn = provenance.connect(self.root / "provenance.sqlite3")
        self.addCleanup(self.conn.close)
        self.policy = load_policy()
        self.now = dt.datetime(2026, 9, 11, tzinfo=dt.timezone.utc)
        self.conn.execute(
            """INSERT INTO turns(session_id,turn_id,end_time,end_hook,end_complete,frozen,
            pruned_at,full_trace_pruned_at,created_at,updated_at) VALUES(
            's','t','2026-07-01T00:00:00Z','Stop',1,0,'yes','yes','now','now')"""
        )
        self.conn.commit()
        self.refs = lambda _row: {
            "in_flight": False,
            "needed_for_retained_evidence": False,
            "needed_for_rollback": False,
        }

        backup_root = self.root / "verified"
        backup_root.mkdir()
        self.proof = verified_backup(self.conn, backup_root, 16 * 1024**2, budget_root=backup_root)

    def test_unknown_references_preserve_old_rows(self):
        manifest, batch = cleanup_manifest(self.conn, self.policy, now=self.now)
        self.assertEqual(manifest["eligible_count"], 0)
        self.assertEqual(batch, [])

    def test_frozen_and_retained_lean_references_preserved(self):
        for refs in (
            {
                "in_flight": False,
                "needed_for_retained_evidence": True,
                "needed_for_rollback": False,
            },
            self.refs(None),
        ):
            if not refs["needed_for_retained_evidence"]:
                self.conn.execute("UPDATE turns SET frozen=1")
                self.conn.commit()
            manifest, _ = cleanup_manifest(
                self.conn, self.policy, lambda _row, refs=refs: refs, self.now
            )
            self.assertEqual(manifest["eligible_count"], 0)

    def test_reference_change_between_manifest_and_transaction_preserves_row(self):
        manifest, batch = cleanup_manifest(self.conn, self.policy, self.refs, self.now)
        self.assertEqual(manifest["eligible_count"], 1)
        self.conn.execute("UPDATE turns SET frozen=1")
        self.conn.commit()
        result = apply_cleanup(self.conn, self.policy, batch, self.refs, self.proof, self.now)
        self.assertEqual(result["applied"], 0)

    def test_backup_restore_and_transactional_delete(self):
        backups = self.root / "backups"
        backups.mkdir()
        proof = verified_backup(self.conn, backups, 16 * 1024**2, budget_root=backups)
        self.assertEqual(proof["restored_rows"], 1)
        self.assertEqual(proof["integrity"], "ok")
        _, batch = cleanup_manifest(self.conn, self.policy, self.refs, self.now)
        result = apply_cleanup(self.conn, self.policy, batch, self.refs, self.proof, self.now)
        self.assertEqual(result["applied"], 1)
        with sqlite3.connect(backups / "metadata-before-cleanup.sqlite3") as restored:
            self.assertEqual(restored.execute("SELECT count(*) FROM turns").fetchone()[0], 1)
        self.assertEqual(list(backups.glob("*.tmp")), [])

    def test_backup_exhaustion_and_interruption_preserve_database(self):
        backups = self.root / "backups"
        backups.mkdir()
        for kwargs in ({"allocation": 1}, {"allocation": 16 * 1024**2, "deadline_seconds": -1}):
            with self.assertRaises(StoragePaused):
                verified_backup(self.conn, backups, budget_root=backups, **kwargs)
            self.assertEqual(list(backups.glob("*.tmp")), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM turns").fetchone()[0], 1)

    def test_required_backup_slot_is_never_overwritten(self):
        backups = self.root / "backups"
        backups.mkdir()
        verified_backup(self.conn, backups, 16 * 1024**2, budget_root=backups)
        with self.assertRaises(StoragePaused):
            verified_backup(self.conn, backups, 16 * 1024**2, budget_root=backups)

    def test_backup_counts_sibling_adoption_and_shares_its_lock(self):
        backups = self.root / "new-backup"
        backups.mkdir()
        (self.root / "required-rollback").write_bytes(b"p" * 1024**2)
        with self.assertRaisesRegex(StoragePaused, "backup_reserve_unavailable"):
            verified_backup(self.conn, backups, 1024**2, budget_root=self.root)
        with (
            exclusive(self.root / ".provenance-adoption.lock"),
            self.assertRaisesRegex(StoragePaused, "another_telemetry_writer_is_active"),
        ):
            verified_backup(self.conn, backups, 16 * 1024**2, budget_root=self.root)
        self.assertFalse(list(backups.glob("*.sqlite3")))

    def test_interrupted_deletion_rolls_back(self):
        def failed(_row):
            raise RuntimeError("reference inventory interrupted")

        with self.assertRaises(RuntimeError):
            apply_cleanup(self.conn, self.policy, [("s", "t")], failed, self.proof, self.now)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM turns").fetchone()[0], 1)

    def test_actual_hook_entrypoint_start_and_stop(self):
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        args = [
            "--db",
            str(self.root / "provenance.sqlite3"),
            "--artifacts",
            str(artifacts),
            "hook",
        ]
        event = {"session_id": "fixture", "turn_id": "turn", "cwd": str(self.root)}
        for name in ("UserPromptSubmit", "Stop"):
            output = io.StringIO()
            with (
                patch("sys.stdin", io.StringIO(json.dumps({**event, "hook_event_name": name}))),
                contextlib.redirect_stdout(output),
            ):
                result = provenance.main(args)
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue()), {})
        row = self.conn.execute("SELECT * FROM turns WHERE session_id='fixture'").fetchone()
        self.assertEqual(row["end_hook"], "Stop")
        self.assertEqual(row["end_complete"], 1)
        self.assertTrue((artifacts / "fixture/turn/identities.json").is_file())

    def test_hook_pressure_does_not_stop_codex(self):
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        output = io.StringIO()
        with exclusive(self.root / ".provenance-writer.lock"), contextlib.redirect_stdout(output):
            code = provenance.main(
                [
                    "--db",
                    str(self.root / "provenance.sqlite3"),
                    "--artifacts",
                    str(artifacts),
                    "hook",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {})


if __name__ == "__main__":
    unittest.main()
