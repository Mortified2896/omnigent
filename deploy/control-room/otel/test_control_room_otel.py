#!/usr/bin/env python3
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("control_room_otel.py")
SPEC = importlib.util.spec_from_file_location("control_room_otel", MODULE_PATH)
OTEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OTEL)


class RetentionTests(unittest.TestCase):
    def test_mac_only_policy_constants(self):
        self.assertEqual(OTEL.ARCHIVE_MAX_AGE_DAYS, 60)
        self.assertEqual(OTEL.ARCHIVE_MAX_BYTES, 50_000_000_000)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "data"
        self.root.mkdir()
        self.now = 2_000_000_000.0

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, relative, size, age_days=0):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        stamp = self.now - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def prune(self, **overrides):
        args = {
            "archive_root": self.root,
            "max_bytes": 10_000,
            "max_age_days": 60,
            "forensic_max_bytes": 10_000,
            "forensic_max_age_days": 3,
            "now": self.now,
        }
        args.update(overrides)
        return OTEL.prune_archive(**args)

    def test_age_policy_deletes_only_old_rotated_files(self):
        old = self.fixture("lean/traces/traces.otlp-old.json", 10, 61)
        young = self.fixture("lean/traces/traces.otlp-young.json", 10, 59)
        forensic_old = self.fixture("forensic/traces/traces.otlp-old.json", 10, 4)
        result = self.prune()
        self.assertFalse(old.exists())
        self.assertFalse(forensic_old.exists())
        self.assertTrue(young.exists())
        self.assertTrue(result["converged"])

    def test_global_size_removes_oldest_rotated_first_and_converges(self):
        oldest = self.fixture("lean/logs/logs.otlp-a.json", 60, 2)
        middle = self.fixture("lean/traces/traces.otlp-b.json", 60, 1)
        newest = self.fixture("lean/metrics/metrics.otlp-c.json", 60, 0)
        result = self.prune(max_bytes=120)
        self.assertFalse(oldest.exists())
        self.assertTrue(middle.exists())
        self.assertTrue(newest.exists())
        self.assertLessEqual(result["after_bytes"], 120)
        self.assertEqual(result["removed"][0]["path"], "lean/logs/logs.otlp-a.json")

    def test_active_unrelated_and_outside_files_are_protected(self):
        active = self.fixture("lean/traces/traces.otlp.json", 80, 100)
        unrelated = self.fixture("notes.txt", 80, 100)
        outside = Path(self.temp.name) / "outside.json"
        outside.write_bytes(b"outside")
        link = self.root / "lean/traces/traces.otlp-link.json"
        link.symlink_to(outside)
        result = self.prune(max_bytes=1)
        self.assertTrue(active.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertFalse(result["converged"])

    def test_forensic_size_is_bounded_independently(self):
        oldest = self.fixture("forensic/traces/traces.otlp-a.json", 60, 2)
        newest = self.fixture("forensic/traces/traces.otlp-b.json", 60, 1)
        result = self.prune(forensic_max_bytes=60)
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())
        self.assertTrue(result["converged"])

    def test_dry_run_and_repeated_runs_are_safe(self):
        candidate = self.fixture("lean/traces/traces.otlp-old.json", 10, 61)
        dry = self.prune(dry_run=True)
        self.assertTrue(candidate.exists())
        self.assertEqual(len(dry["removed"]), 1)
        first = self.prune()
        second = self.prune()
        self.assertEqual(len(first["removed"]), 1)
        self.assertEqual(second["removed"], [])
        self.assertTrue(second["converged"])

    def test_exact_age_and_size_boundaries_are_retained(self):
        lean = self.fixture("lean/logs/logs.otlp-boundary.json", 40, 60)
        forensic = self.fixture("forensic/traces/traces.otlp-boundary.json", 60, 3)
        result = self.prune(max_bytes=100, forensic_max_bytes=60)
        self.assertTrue(lean.exists())
        self.assertTrue(forensic.exists())
        self.assertEqual(result["removed"], [])

    def test_shared_budget_includes_both_producers_and_forensic(self):
        desktop = self.fixture("lean/logs/logs.otlp-desktop.json", 40, 2)
        omnigent = self.fixture("lean/logs/logs.otlp-omnigent.json", 40, 1)
        forensic = self.fixture("forensic/traces/traces.otlp-young.json", 40)
        result = self.prune(max_bytes=80, forensic_max_bytes=40)
        self.assertFalse(desktop.exists())
        self.assertTrue(omnigent.exists())
        self.assertTrue(forensic.exists())
        self.assertEqual(result["after_bytes"], 80)

    def test_active_forensic_causes_visible_nonconvergence(self):
        active = self.fixture("forensic/traces/traces.otlp.json", 80, 100)
        result = self.prune(max_bytes=40, forensic_max_bytes=20)
        self.assertTrue(active.exists())
        self.assertFalse(result["converged"])
        self.assertEqual(result["after_bytes"], 80)


if __name__ == "__main__":
    unittest.main()
