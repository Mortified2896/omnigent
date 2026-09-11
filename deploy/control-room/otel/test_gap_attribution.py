"""Bounded gap reasons remain independent of normal Codex execution."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

import codex_otel_decisions as decisions
from managed_storage import GAP_REASONS, StoragePaused, exclusive, gap_reason, management_status
from pressure_managed_target import Fixture


class GapAttributionTests(unittest.TestCase):
    def setUp(self):
        import sys

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Fixture(self.tmp.name, sys.executable)
        self.fixture.status()
        self.path = self.fixture.state / "managed-target-state.json"

    def record(self, reason):
        return management_status(
            self.fixture.home, policy=self.fixture.policy, gap=True, reason=reason
        )

    def test_legacy_counts_remain_unknown_and_refresh_preserves_reasons(self):
        prior = json.loads(self.path.read_text())
        prior.pop("optional_gap_reasons", None)
        prior["skipped_optional_hooks"] = 45
        self.path.write_text(json.dumps(prior))
        result = self.record("writer_lock_busy")
        self.assertEqual(result["skipped_optional_hooks"], 46)
        self.assertEqual(result["optional_gap_reasons"]["unknown"], 45)
        self.assertEqual(result["optional_gap_reasons"]["writer_lock_busy"], 1)
        refreshed = self.fixture.status()
        self.assertEqual(refreshed["optional_gap_reasons"], result["optional_gap_reasons"])
        self.assertEqual(refreshed["last_optional_gap_at"], result["last_optional_gap_at"])

    def test_untrusted_fields_are_bounded_and_never_persisted(self):
        prior = json.loads(self.path.read_text())
        prior.update(
            skipped_optional_hooks=2,
            optional_gap_reasons={"/private/prompt": 2},
            last_optional_gap_reason="secret exception",
        )
        self.path.write_text(json.dumps(prior))
        result = self.record("/private/prompt")
        self.assertEqual(set(result["optional_gap_reasons"]), set(GAP_REASONS))
        self.assertEqual(result["optional_gap_reasons"]["unknown"], 3)
        self.assertNotIn("private", self.path.read_text())
        self.assertNotIn("secret", self.path.read_text())
        self.assertLess(self.path.stat().st_size, 65536)

    def test_saturation_and_malformed_counts(self):
        prior = json.loads(self.path.read_text())
        prior.update(skipped_optional_hooks=2**63 - 1, optional_gap_reasons=[])
        self.path.write_text(json.dumps(prior))
        result = self.record("managed_pause")
        self.assertEqual(sum(result["optional_gap_reasons"].values()), 2**63 - 1)
        self.assertEqual(result["skipped_optional_hooks"], 2**63 - 1)

    def test_classifier_only_emits_fixed_labels(self):
        for error, expected in [
            (StoragePaused("another_telemetry_writer_is_active"), "writer_lock_busy"),
            (StoragePaused("capture_allocation_full"), "storage_allocation"),
            (StoragePaused("wal_reader_pinned"), "sqlite_or_wal"),
            (sqlite3.OperationalError("private path"), "sqlite_or_wal"),
            (OSError("private payload"), "unknown"),
        ]:
            self.assertEqual(gap_reason(error), expected)

    def test_real_hook_contention_and_pause_are_distinct(self):
        fixture = self.fixture
        with exclusive(fixture.home / ".provenance-writer.lock"):
            fixture.hook("busy", False)
        result = json.loads(self.path.read_text())
        self.assertEqual(result["optional_gap_reasons"]["writer_lock_busy"], 1)
        fixture.fill_to(48_200_000)
        fixture.hook("paused", False)
        result = json.loads(self.path.read_text())
        self.assertEqual(result["optional_gap_reasons"]["managed_pause"], 1)
        self.assertEqual(result["skipped_optional_hooks"], 2)

    def test_reconcile_pause_is_not_a_hook_failure(self):
        import subprocess

        self.fixture.fill_to(48_200_000)
        command = self.fixture.hook_command()
        command[-1] = "reconcile"
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        counts = json.loads(self.path.read_text())["optional_gap_reasons"]
        self.assertEqual(counts["reconcile_skipped"], 1)
        self.assertEqual(counts["managed_pause"], 0)

    def test_database_failure_inside_hook_is_counted_once(self):
        event = {"session_id": "test", "turn_id": "test", "hook_event_name": "Stop"}
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(event))),
            mock.patch.object(
                decisions, "connect", side_effect=sqlite3.OperationalError("private path")
            ),
            mock.patch.object(decisions, "POLICY", self.fixture.policy),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                decisions.hook(self.fixture.db, self.fixture.artifacts, self.fixture.home), 0
            )
        result = json.loads(self.path.read_text())
        self.assertEqual(result["skipped_optional_hooks"], 1)
        self.assertEqual(result["optional_gap_reasons"]["sqlite_or_wal"], 1)

    def test_hook_failure_survives_attribution_failure(self):
        with (
            mock.patch("sys.stdin", io.StringIO("invalid json")),
            mock.patch.object(decisions, "management_status", side_effect=OSError("full")),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(decisions.hook(self.fixture.db, self.fixture.artifacts), 0)
        self.assertEqual(output.getvalue(), "{}\n")

    def test_locked_attribution_is_unavailable_not_zero(self):
        with exclusive(self.fixture.state / ".managed-target.lock"):
            result = self.record("storage_allocation")
        self.assertEqual(result["gap_reporting"], "unavailable")
        self.assertTrue(result["optional_capture_gap"])
