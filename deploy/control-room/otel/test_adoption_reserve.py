"""Real temporary-file pressure checks; no Mac installation or service is used."""

from __future__ import annotations

import contextlib
import hashlib
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adopt_macos as adopt
import managed_adoption as admission
import managed_budget
from managed_storage import StoragePaused, measure


class AdoptionReserveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.home = self.root / "otel"
        self.sidecar = self.root / "provenance"
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        (self.sidecar / "bin").mkdir(parents=True)
        (self.sidecar / "provenance.sqlite3").write_bytes(b"do not touch database")
        for name, relative in adopt.FILES.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("candidate " + name).encode())
            path.chmod(0o755 if name.endswith(".py") else 0o644)
            old = self.home / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(("original " + name).encode())
        for name in adopt.PROVENANCE_FILES:
            (self.source / name).write_bytes(("candidate " + name).encode())
        self.script = self.sidecar / "bin/codex_otel_decisions.py"
        self.script.write_bytes(b"original sidecar")
        (self.state / "capture_node_id").write_text("fixture-identity")
        (self.home / "bin/otelcol-contrib").write_text("fixture-not-executable")
        self.plist = self.root / "collector.plist"
        self.plist.write_bytes(plistlib.dumps({"ProgramArguments": [
            str(self.home / "bin/otelcol-contrib"), "--config",
            str(self.home / "config/otelcol-macos.yaml")
        ]}))
        self.limit = 32 * 1024**2
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for module in (admission, managed_budget):
            stack.enter_context(patch.object(module, "load_policy", side_effect=lambda: {
                "allocations": {"telemetry_backups": self.limit}
            }))
        stack.enter_context(patch.object(adopt, "source_status", side_effect=lambda source: {
            "dirty": False, "repository": "Mortified2896/omnigent", "commit": "fixture",
            "files": {name: adopt.digest(source / name) for name in adopt.FILES}
        }))

    def generic(self):
        # Only the external pinned-Collector config-validation process is mocked.
        with patch.object(adopt.subprocess, "run") as validate:
            result = adopt.adopt(self.source, self.home, self.plist)
        self.assertEqual(validate.call_count, 1)
        return result

    def provenance(self):
        return adopt.adopt_provenance(
            self.source, self.sidecar, self.home, adopt.digest(self.script)
        )

    def installed(self):
        paths = [self.home / value for value in adopt.FILES.values()]
        paths += [self.script, self.sidecar / "provenance.sqlite3"]
        return {str(path): path.read_bytes() for path in paths}

    def assert_no_backup(self):
        self.assertFalse(list(self.state.glob("*rollback-*")))

    def test_generic_refuses_full_reserve_before_replacement(self):
        self.limit = 1
        before = self.installed()
        with self.assertRaisesRegex(StoragePaused, "adoption_reserve_unavailable"):
            self.generic()
        self.assertEqual(before, self.installed())
        self.assert_no_backup()

    def test_sidecar_refuses_full_reserve_before_replacement(self):
        self.limit = 1
        before = self.installed()
        with self.assertRaisesRegex(StoragePaused, "adoption_reserve_unavailable"):
            self.provenance()
        self.assertEqual(before, self.installed())
        self.assert_no_backup()

    def test_old_sidecar_not_new_source_size_determines_backup_need(self):
        self.script.write_bytes(b"x" * (4 * 1024**2))
        self.limit = 3 * 1024**2
        before = self.installed()
        with self.assertRaises(StoragePaused):
            self.provenance()
        self.assertEqual(before, self.installed())
        self.assert_no_backup()

    def test_existing_backups_are_charged_and_never_deleted(self):
        saved = self.state / "operator-retained.backup"
        saved.write_bytes(b"x" * 1024**2)
        self.limit = 1024**2
        with self.assertRaises(StoragePaused):
            self.generic()
        self.assertEqual(saved.stat().st_size, 1024**2)
        self.assert_no_backup()

    def test_unknown_inventory_cannot_mean_empty(self):
        with patch.object(admission, "measure", return_value={"complete": False, "components": None}):
            with self.assertRaisesRegex(StoragePaused, "adoption_inventory_unknown"):
                self.generic()
        self.assert_no_backup()

    def test_generic_success_keeps_config_identity_and_rollback(self):
        config = self.home / "config/otelcol-macos.yaml"
        config.write_bytes((self.source / "config/otelcol-macos.yaml").read_bytes())
        signature = admission._signature(config.stat())
        originals = self.installed()
        result = self.generic()
        self.assertFalse(result["restarted"])
        self.assertEqual(admission._signature(config.stat()), signature)
        helper = self.home / "bin/control_room_otel.py"
        self.assertEqual(helper.read_bytes(), (self.source / "control_room_otel.py").read_bytes())
        self.assertTrue(helper.stat().st_mode & 0o100)
        subprocess.run([sys.executable, result["rollback"]], check=True, capture_output=True)
        self.assertEqual(originals, self.installed())
        self.assertEqual(admission._signature(config.stat()), signature)

    def test_sidecar_success_keeps_database_and_rollback(self):
        before = self.script.read_bytes()
        result = self.provenance()
        self.assertFalse(result["total_enforcement_verified"])
        subprocess.run([sys.executable, result["rollback"]], check=True, capture_output=True)
        self.assertEqual(before, self.script.read_bytes())
        self.assertEqual(list((self.sidecar / "bin").iterdir()), [self.script])
        self.assertEqual((self.sidecar / "provenance.sqlite3").read_bytes(), b"do not touch database")

    def test_both_adopters_share_real_process_lock(self):
        program = (
            "import fcntl,sys; f=open(sys.argv[1],'a'); "
            "fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program, str(self.state / ".provenance-adoption.lock")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            for call in (self.generic, self.provenance):
                with self.subTest(call=call.__name__), self.assertRaises(StoragePaused):
                    call()
            self.assert_no_backup()
        finally:
            child.communicate("release\n", timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(self.generic()["restarted"])

    def test_snapshot_copy_does_not_follow_later_source_growth(self):
        source = self.source / "control_room_otel.py"
        expected = source.read_bytes()
        with admission.reserved_copies([source], [], self.state, [self.home]) as copies:
            source.write_bytes(b"bigger" * 10000)
            copies.copy2(source, self.home / "snapshot-copy")
        self.assertEqual((self.home / "snapshot-copy").read_bytes(), expected)

    def test_reviewed_hash_mismatch_is_refused(self):
        source = self.source / "control_room_otel.py"
        with admission.reserved_copies([source], [], self.state, [self.home]) as copies:
            with self.assertRaisesRegex(StoragePaused, "reviewed_source_changed"):
                copies.assert_hashes({source: "0" * 64})
        self.assert_no_backup()

    def test_unknown_candidate_hash_cannot_be_skipped(self):
        source = self.source / "control_room_otel.py"
        with admission.reserved_copies([source], [], self.state, [self.home]) as copies:
            copies.assert_hashes({source: hashlib.sha256(source.read_bytes()).hexdigest()})
            with self.assertRaises(StoragePaused):
                copies.assert_hashes({self.source / "missing": "0" * 64})

    def test_changed_input_signature_is_detected(self):
        source = self.source / "control_room_otel.py"
        with admission.reserved_copies([source], [], self.state, [self.home]) as copies:
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(StoragePaused, "adoption_source_changed"):
                copies.assert_unchanged()

    def test_symlinked_candidate_is_refused(self):
        source = self.source / "control_room_otel.py"
        source.unlink()
        source.symlink_to(self.script)
        with self.assertRaises(StoragePaused):
            self.generic()
        self.assert_no_backup()

    def test_parent_symlink_is_refused(self):
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(StoragePaused):
            with admission.reserved_copies([], [], alias, [self.home]):
                self.fail("unsafe state admitted")

    def test_hardlinked_candidate_is_refused(self):
        source = self.source / "control_room_otel.py"
        os.link(source, self.source / "external-alias")
        with self.assertRaises(StoragePaused):
            self.generic()
        self.assert_no_backup()

    def test_fifo_candidate_is_refused_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(StoragePaused, "unsafe_adoption_source"):
            admission._image(fifo)

    def test_missing_required_and_optional_sources_differ(self):
        path = self.root / "missing"
        self.assertIsNone(admission._image(path, optional=True))
        with self.assertRaises(StoragePaused):
            admission._image(path)

    def test_oversized_candidate_is_refused_before_copies(self):
        with patch.object(admission, "_MAX_IMAGE_BYTES", 4), self.assertRaises(StoragePaused):
            self.generic()
        self.assert_no_backup()

    def test_total_snapshot_memory_is_bounded(self):
        with patch.object(admission, "_MAX_IMAGES_BYTES", 1), self.assertRaises(StoragePaused):
            self.generic()
        self.assert_no_backup()

    def test_existing_pending_file_is_not_overwritten(self):
        pending = self.home / "bin/control_room_otel.py.pending"
        pending.write_bytes(b"unrelated pending work")
        with self.assertRaises(ValueError):
            self.generic()
        self.assertEqual(pending.read_bytes(), b"unrelated pending work")
        self.assert_no_backup()

    def test_copy_cannot_escape_admitted_destination(self):
        source = self.source / "control_room_otel.py"
        outside = self.root / "outside"
        with admission.reserved_copies([source], [], self.state, [self.home]) as copies:
            with self.assertRaises(StoragePaused):
                copies.copy2(source, outside)
        self.assertFalse(outside.exists())

    def test_control_output_and_total_written_bytes_are_bounded(self):
        with admission.reserved_copies([], [], self.state, [self.home]) as copies:
            with self.assertRaises(StoragePaused):
                copies.text(self.home / "huge.json", "x" * 65537)
            copies.required_bytes = 1
            with self.assertRaises(StoragePaused):
                copies.text(self.home / "over-limit.json", "{}")
        self.assertFalse((self.home / "huge.json").exists())
        self.assertFalse((self.home / "over-limit.json").exists())

    def test_success_does_not_delete_old_backups_or_capture(self):
        saved = self.state / "required-rollback.bin"
        saved.write_bytes(b"protected")
        archive = self.home / "data"
        archive.mkdir()
        (archive / "trace.bin").write_bytes(b"keep capture")
        with patch.object(Path, "unlink", side_effect=AssertionError("no deletion allowed")):
            self.generic()
        self.assertEqual(saved.read_bytes(), b"protected")
        self.assertEqual((archive / "trace.bin").read_bytes(), b"keep capture")

    def test_later_adoption_rechecks_accumulated_backup_bytes(self):
        self.generic()
        before = self.installed()
        used = measure([{"path": str(self.state), "component": "telemetry_backups"}])
        self.limit = used["components"]["telemetry_backups"]["bytes"]
        with self.assertRaises(StoragePaused):
            self.generic()
        self.assertEqual(self.installed(), before)
        self.assertEqual(len(list(self.state.glob("rollback-*"))), 1)


if __name__ == "__main__":
    unittest.main()
