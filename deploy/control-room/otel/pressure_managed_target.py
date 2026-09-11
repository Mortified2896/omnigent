"""Real cooperating hook/retention pressure exercise in temporary small storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from managed_budget import load_policy
from managed_storage import management_status, measure


class Fixture:
    def __init__(self, root, python):
        self.root = Path(root).resolve()
        self.python = python
        self.source = Path(__file__).resolve().parent
        self.home = self.root / "telemetry"
        self.bin = self.home / "bin"
        self.state = self.home / "state"
        self.archive = self.home / "data"
        self.artifacts = self.home / "artifacts"
        for path in (self.bin, self.state, self.archive / "lean/traces", self.artifacts):
            path.mkdir(parents=True)
        for name in (
            "managed_budget.py",
            "managed_storage.py",
            "managed_sqlite.py",
            "managed_metadata.py",
            "managed_diagnostics.py",
            "codex_otel_decisions.py",
            "control_room_otel.py",
        ):
            shutil.copyfile(self.source / name, self.bin / name)
        self.policy = load_policy()
        self.policy["total_max_bytes"] //= 1000
        self.policy["forensic_max_bytes"] //= 1000
        for section in ("allocations", "management"):
            self.policy[section] = {k: v // 1000 for k, v in self.policy[section].items()}
        self.policy["allocations"].update(telemetry_logs=1_000_000, other_managed=256_000)
        (self.bin / "managed_storage_policy.json").write_text(json.dumps(self.policy))
        self.roots = [
            {
                "path": str(self.home),
                "component": "other_managed",
                "prefixes": {
                    "data": "otel_archive",
                    "artifacts": "provenance_captures",
                    "state": "telemetry_backups",
                    "bin": "telemetry_runtime",
                    "provenance.sqlite3": "provenance_database",
                },
            }
        ]
        (self.state / "managed-roots.json").write_text(json.dumps(self.roots))
        self.protected = self.home / "frozen-inflight-rollback-evidence"
        self.protected.write_bytes(b"protected fixture evidence\n")
        self.protected_hash = hashlib.sha256(self.protected.read_bytes()).hexdigest()
        self.pressure = self.home / "owner-held-pressure"
        self.db = self.home / "provenance.sqlite3"
        self.events = []

    def status(self):
        return management_status(self.home, policy=self.policy, refresh=True)

    def fill_to(self, desired):
        self.pressure.touch()
        scan = measure(self.roots)
        assert scan["complete"], scan["errors"]
        current = sum(v["bytes"] for v in scan["components"].values())
        base = current - max(self.pressure.stat().st_size, self.pressure.stat().st_blocks * 512)
        # Sparse owner-owned pressure counts logically without filling the disk.
        self.pressure.unlink()
        with self.pressure.open("wb") as stream:
            stream.truncate(max(0, desired - base))
        return self.status()

    def hook_command(self):
        return [
            self.python,
            "-B",
            str(self.bin / "codex_otel_decisions.py"),
            "--management-home",
            str(self.home),
            "--db",
            str(self.db),
            "--artifacts",
            str(self.artifacts),
            "hook",
        ]

    def hook(self, turn, expected_capture):
        event = {
            "session_id": "managed-target-fixture",
            "turn_id": turn,
            "cwd": str(self.root),
            "hook_event_name": "UserPromptSubmit",
        }
        proc = subprocess.run(
            self.hook_command(),
            input=json.dumps(event),
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert proc.returncode == 0 and proc.stdout.strip() == "{}", proc.stderr
        captured = (self.artifacts / "managed-target-fixture" / turn / "identities.json").exists()
        assert captured == expected_capture, (turn, captured, proc.stderr)
        self.events.append(
            {"turn": turn, "exit_code": proc.returncode, "optional_artifact_created": captured}
        )
        return proc

    def retain(self):
        proc = subprocess.run(
            [self.python, "-B", str(self.bin / "control_room_otel.py"), "retain"],
            env={**os.environ, "CONTROL_ROOM_OTEL_HOME": str(self.home)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode in (0, 1), proc.stderr + proc.stdout
        return json.loads(proc.stdout)

    def run(self):
        transitions = []

        def expect(result, state):
            assert result["management_state"] == state, result
            assert result["normal_codex_allowed"] is True
            assert result["hard_ceiling_enforced"] is False
            transitions.append({"state": state, "bytes": result["used_bytes"]})

        expect(self.status(), "NORMAL")
        self.hook("normal", True)
        expect(self.fill_to(45_200_000), "CLEANUP_DUE")
        self.hook("cleanup-due", True)
        expect(self.fill_to(48_200_000), "OPTIONAL_PAUSED")
        self.hook("paused", False)
        expect(self.fill_to(50_200_000), "OVER_TARGET")
        self.hook("over-target", False)
        expect(self.fill_to(46_500_000), "OPTIONAL_PAUSED")
        self.hook("hysteresis", False)
        # Already-authorized closed archive candidate; active and unknown files stay.
        candidate = self.archive / "lean/traces/traces.otlp-fixture.json"
        candidate.write_bytes(b"x" * 1_000_000)
        active = self.archive / "lean/traces/traces.otlp.json"
        active.write_text("{}\n")
        self.status()
        retained = self.retain()
        assert not candidate.exists() and active.read_text() == "{}\n", retained
        assert retained["removed_count"] == 1, retained
        # Owner releases its protected pressure file; retention never deletes it.
        expect(self.fill_to(45_800_000), "CLEANUP_DUE")
        self.hook("resumed", True)
        expect(self.fill_to(44_500_000), "NORMAL")
        self.hook("normal-again", True)
        final = self.status()
        assert final["skipped_optional_hooks"] == 3 and final["optional_capture_gap"]
        assert hashlib.sha256(self.protected.read_bytes()).hexdigest() == self.protected_hash
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("SELECT turn_id FROM turns ORDER BY turn_id").fetchall()
        assert len(rows) == 4, rows
        return {
            "transitions": transitions,
            "hooks": self.events,
            "cleanup": retained,
            "protected_evidence_unchanged": True,
            "optional_rows": len(rows),
            "gap_count": final["skipped_optional_hooks"],
            "gap_persists_after_resume": final["optional_capture_gap"],
            "normal_codex_allowed": True,
            "hard_ceiling_enforced": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="/usr/bin/python3")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="managed-target-") as directory:
        print(json.dumps(Fixture(directory, args.python).run(), indent=2))


if __name__ == "__main__":
    main()
