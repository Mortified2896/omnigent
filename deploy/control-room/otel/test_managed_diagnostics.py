"""Exercise real child descriptors and in-flight disk use in temporary roots."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from managed_diagnostics import supervise
from managed_storage import BoundedLog, StoragePaused, exclusive, measure


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.allocation = 2 * 1024**2

    def run_child(self, code):
        return supervise(
            [sys.executable, "-c", code],
            self.root,
            "collector",
            self.allocation,
            segment_bytes=65536,
        )

    def test_real_open_descriptors_rotate_without_unlinked_growth(self):
        code = 'import os;\nfor _ in range(256):\n os.write(1,b"x"*8192); os.write(2,b"y"*8192)'
        self.assertEqual(self.run_child(code), 0)
        status = json.loads((self.root / "managed-collector-status.json").read_text())
        self.assertEqual(status["dropped_bytes"], 0)
        self.assertEqual(status["accepted_bytes"], 4 * 1024**2)
        self.assertEqual(status["state"], "EXITED")
        for path in self.root.glob("*.log*"):
            self.assertLessEqual(path.stat().st_size, 65536)

    def test_protected_fullness_keeps_child_successful_and_recovers(self):
        protected = self.root / "required-legacy.log"
        protected.write_bytes(b"p" * (self.allocation - 200000))
        self.assertEqual(self.run_child('import os; os.write(1,b"x"*1000000)'), 0)
        status = json.loads((self.root / "managed-collector-status.json").read_text())
        self.assertGreater(status["dropped_bytes"], 0)
        self.assertEqual(protected.stat().st_size, self.allocation - 200000)
        protected.unlink()  # Fixture owner simulates separately authorized cleanup.
        self.assertEqual(self.run_child('print("recovered")'), 0)
        self.assertIn("recovered", (self.root / "managed-collector-stdout.log").read_text())

    def test_failure_to_rotate_refuses_the_write(self):
        log = BoundedLog(self.root / "log", segment_bytes=16, backups=1)
        log.write("a" * 16)
        (self.root / "log.1").write_bytes(b"old")
        with (
            patch.object(Path, "unlink", side_effect=PermissionError("cleanup failed")),
            self.assertRaises(PermissionError),
        ):
            log.write("b")
        self.assertEqual((self.root / "log").read_bytes(), b"a" * 16)

    def test_missing_inventory_and_oversized_record(self):
        log = BoundedLog(self.root / "log", segment_bytes=64, allocation=self.allocation)
        log.write("z" * 1000000)
        self.assertLessEqual((self.root / "log").stat().st_size, 64)
        (self.root / "unsafe").symlink_to(self.root / "absent")
        with self.assertRaisesRegex(StoragePaused, "log_inventory_unknown"):
            log.write("refused")

    def test_competing_real_producers_share_allocation_during_writes(self):
        module = str(Path(__file__).parent)
        program = """import sys,time
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from managed_storage import BoundedLog,StoragePaused
log=BoundedLog(Path(sys.argv[2])/sys.argv[3],segment_bytes=65536,allocation=1048576)
for _ in range(200):
 try: log.write("x"*16384)
 except StoragePaused: pass
 time.sleep(.002)
"""
        children = [
            subprocess.Popen([sys.executable, "-c", program, module, str(self.root), name])
            for name in ("one.log", "two.log", "three.log")
        ]
        peak = 0
        try:
            while any(child.poll() is None for child in children):
                scan = measure([{"path": str(self.root), "component": "telemetry_logs"}])
                if scan["components"]:
                    peak = max(peak, scan["components"]["telemetry_logs"]["bytes"])
                time.sleep(0.001)
            self.assertEqual([child.wait() for child in children], [0, 0, 0])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        self.assertGreater(peak, 100000)
        self.assertLessEqual(peak, 1048576)

    def test_stalled_other_writer_does_not_block_child(self):
        with exclusive(self.root / ".managed-logs.lock"):
            started = time.monotonic()
            self.assertEqual(self.run_child('import os; os.write(2,b"x"*2000000)'), 0)
            self.assertLess(time.monotonic() - started, 10)
        self.assertFalse((self.root / "managed-collector-stderr.log").exists())


if __name__ == "__main__":
    unittest.main()
