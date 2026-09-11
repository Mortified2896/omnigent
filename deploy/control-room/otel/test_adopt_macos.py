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
    def test_status_helper_direct_invocation(self):
        helper = Path(__file__).with_name("control_room_otel.py")
        result = subprocess.run([str(helper), "--help"], capture_output=True, check=True)
        self.assertIn(b"status", result.stdout)

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
            config = home / "config/otelcol-macos.yaml"
            config.write_text("candidate")
            config_before = config.stat()
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
            config_after = config.stat()
            self.assertEqual(
                (config_before.st_ino, config_before.st_mtime_ns, config_before.st_ctime_ns),
                (config_after.st_ino, config_after.st_mtime_ns, config_after.st_ctime_ns),
            )
            self.assertEqual((home / "bin/control_room_otel.py").read_text(), "candidate")
            self.assertEqual((home / "state/capture_node_id").read_text(), "stable-capture")
            subprocess.run(["python3", result["rollback"]], check=True, capture_output=True)
            self.assertEqual(config.stat().st_ctime_ns, config_before.st_ctime_ns)
            for relative in ADOPT.FILES.values():
                expected = "candidate" if relative == "config/otelcol-macos.yaml" else "original"
                self.assertEqual((home / relative).read_text(), expected)
            self.assertTrue(
                json.loads((home / "state/source-manifest.json").read_text())["runtime_verified"]
                is False
            )


class ProvenanceAdoptionTests(unittest.TestCase):
    def test_source_only_adoption_and_verified_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, home, otel = root / "source", root / "provenance", root / "otel"
            source.mkdir()
            (home / "bin").mkdir(parents=True)
            (otel / "state").mkdir(parents=True)
            (home / "provenance.sqlite3").write_bytes(b"preserved database fixture")
            script = home / "bin/codex_otel_decisions.py"
            script.write_bytes(b"old source")
            before = ADOPT.digest(script)
            for name in ADOPT.PROVENANCE_FILES:
                (source / name).write_text("candidate " + name)
            with patch.object(
                ADOPT,
                "source_status",
                return_value={
                    "dirty": False,
                    "repository": "Mortified2896/omnigent",
                    "commit": "fixture",
                },
            ):
                result = ADOPT.adopt_provenance(source, home, otel, before)
            self.assertFalse(result["total_enforcement_verified"])
            self.assertFalse(result["services_restarted"])
            subprocess.run(["python3", result["rollback"]], check=True, capture_output=True)
            self.assertEqual(ADOPT.digest(script), before)
            self.assertEqual(list((home / "bin").iterdir()), [script])
            self.assertEqual(
                (home / "provenance.sqlite3").read_bytes(), b"preserved database fixture"
            )
