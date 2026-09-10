"""Source-selection and rollback fixtures never touch a live installation."""

import importlib.util
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("adopt", Path(__file__).with_name("adopt_macos.py"))
ADOPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADOPT)


class AdoptionTests(unittest.TestCase):
    def test_rejects_retired_source_before_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "deploy/control-room/otel"
            source.mkdir(parents=True)
            with (
                patch.object(
                    ADOPT.subprocess,
                    "check_output",
                    side_effect=[
                        str(root),
                        "git@github.com:Mortified2896/control-room-standalone.git",
                    ],
                ),
                self.assertRaisesRegex(ValueError, "unexpected source"),
            ):
                ADOPT.source_status(source)

    def test_existing_installation_backup_and_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            home = root / "installed"
            for name, relative in ADOPT.FILES.items():
                candidate = source / name
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text("candidate")
                target = home / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("original")
            (home / "state").mkdir()
            (home / "state/capture_node_id").write_text("stable-capture")
            (home / "bin/otelcol-contrib").write_text("pinned-binary")
            plist = root / "collector.plist"
            plist.write_bytes(
                plistlib.dumps(
                    {
                        "ProgramArguments": [
                            str(home / "bin/otelcol-contrib"),
                            "--config",
                            str(home / "config/otelcol-macos.yaml"),
                        ]
                    }
                )
            )
            with (
                patch.object(ADOPT, "source_status", return_value={"files": {}}),
                patch.object(ADOPT.subprocess, "run"),
            ):
                result = ADOPT.adopt(source, home, plist)
            self.assertFalse(result["restarted"])
            self.assertEqual((home / "state/capture_node_id").read_text(), "stable-capture")
            subprocess.run(["python3", result["rollback"]], check=True, capture_output=True)
            for relative in ADOPT.FILES.values():
                self.assertEqual((home / relative).read_text(), "original")
            self.assertTrue(
                json.loads((home / "state/source-manifest.json").read_text())["runtime_verified"]
                is False
            )
