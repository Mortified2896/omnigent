"""Real hook subprocesses, cleanup and persisted hysteresis at small budgets."""

import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest

from managed_storage import exclusive, management_status
from pressure_managed_target import Fixture


class ManagedTargetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Fixture(self.tmp.name, sys.executable)

    def test_real_hooks_cleanup_and_recovery(self):
        result = self.fixture.run()
        self.assertEqual(result["gap_count"], 3)
        self.assertTrue(result["protected_evidence_unchanged"])

    def test_stale_inventory_pauses_hook_without_database_write(self):
        fixture = self.fixture
        fixture.status()
        path = fixture.state / "managed-target-state.json"
        data = json.loads(path.read_text())
        data["observed_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        ).isoformat()
        path.write_text(json.dumps(data))
        fixture.hook("stale", False)
        self.assertFalse(fixture.db.exists())
        self.assertTrue(json.loads(path.read_text())["optional_capture_gap"])

    def test_concurrent_control_lock_does_not_block_hook(self):
        fixture = self.fixture
        fixture.fill_to(48_200_000)
        with exclusive(fixture.state / ".managed-target.lock"):
            fixture.hook("contended", False)
        self.assertFalse(fixture.db.exists())

    def test_diagnostic_child_completes_while_optional_logs_pause_and_resume(self):
        fixture = self.fixture
        (fixture.home / "collector-logs").mkdir()
        for desired, paused in ((48_200_000, True), (45_800_000, False)):
            fixture.fill_to(desired)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(fixture.bin / "managed_diagnostics.py"),
                    "--home",
                    str(fixture.home),
                    "--name",
                    "retention",
                    "--",
                    sys.executable,
                    "-c",
                    "print('PRIMARY_CHILD_COMPLETED')",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            status = json.loads(
                (fixture.home / "collector-logs/managed-retention-status.json").read_text()
            )
            self.assertGreater(status["dropped_bytes"], 0)
            if paused:
                self.assertEqual(status["accepted_bytes"], 0)
                self.assertEqual(status["last_pause"], "managed_target_optional_pause")
            else:
                self.assertGreater(status["accepted_bytes"], 0)

    def test_missing_root_and_corrupt_state_are_visible(self):
        fixture = self.fixture
        fixture.status()
        (fixture.state / "managed-roots.json").write_text("[]")
        result = fixture.status()
        self.assertEqual(result["management_state"], "INCOMPLETE")
        self.assertTrue(result["normal_codex_allowed"])
        self.assertTrue(result["optional_capture_gap"])
        fixture.hook("missing-root-after-normal", False)
        self.assertFalse(fixture.db.exists())
        (fixture.state / "managed-target-state.json").write_text("[]")
        self.assertEqual(management_status(fixture.home)["management_state"], "INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
