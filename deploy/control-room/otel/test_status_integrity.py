"""Offline evidence tests; no Collector, Codex process or live archive is used."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import control_room_otel as otel

NAME = "otelcol_exporter_send_failed_spans"
NOW = 2_000_000_000.0


class CounterTests(unittest.TestCase):
    def test_absent_is_not_zero(self):
        self.assertIsNone(otel.metric("# only metadata\n", NAME))

    def test_observed_zero_is_zero(self):
        self.assertEqual(otel.metric(f"{NAME} 0\n", NAME), 0)

    def test_total_suffix_is_supported(self):
        self.assertEqual(otel.metric(f'{NAME}_total{{exporter="file"}} 7\n', NAME), 7)

    def test_timestamp_is_not_used_as_counter_value(self):
        self.assertEqual(otel.metric(f"{NAME} 7 1234567890000\n", NAME), 7)

    def test_labels_whitespace_escapes_and_braces(self):
        body = f'{NAME}{{note="a }} b \\\" c",exporter="file"}}\t7\t123\n'
        self.assertEqual(otel.metric(body, NAME), 7)

    def test_sum_distinct_series(self):
        body = f'{NAME}{{exporter="a"}} 2\n{NAME}{{exporter="b"}} 3\n'
        self.assertEqual(otel.metric(body, NAME), 5)

    def test_missing_signal_is_not_zero(self):
        body = f'{NAME}{{type="logs"}} 0\n'
        self.assertIsNone(otel.metric(body, NAME, "traces"))

    def test_signal_matching_is_not_substring_matching(self):
        body = f'{NAME}{{prototype="traces"}} 7\n'
        self.assertIsNone(otel.metric(body, NAME, "traces"))

    def test_signal_aliases_are_supported(self):
        for label in ("type", "data_type"):
            with self.subTest(label=label):
                self.assertEqual(otel.metric(f'{NAME}{{{label}="traces"}} 2\n', NAME, "traces"), 2)

    def test_invalid_counter_values_remain_unknown(self):
        for value in ("NaN", "+Inf", "-Inf", "not-a-number", "-1", "0.5", "1e999999999"):
            with self.subTest(value=value):
                self.assertIsNone(otel.metric(f"{NAME} {value}\n", NAME))

    def test_bad_series_does_not_disappear_from_sum(self):
        body = f'{NAME}{{exporter="a"}} 2\n{NAME}{{exporter="b"}} invalid\n'
        self.assertIsNone(otel.metric(body, NAME))

    def test_integer_precision_and_exponent(self):
        self.assertEqual(otel.metric(f"{NAME} 9007199254740993\n", NAME), 9007199254740993)
        self.assertEqual(otel.metric(f"{NAME} 1e3\n", NAME), 1000)

    def test_duplicate_series_is_unknown_not_double_counted(self):
        body = f'{NAME}{{a="1",b="2"}} 3\n{NAME}{{b="2",a="1"}} 3\n'
        self.assertIsNone(otel.metric(body, NAME))

    def test_two_alias_families_are_not_summed(self):
        self.assertIsNone(otel.metric(f"{NAME} 3\n{NAME}_total 3\n", NAME))

    def test_invalid_or_duplicate_labels_are_unknown(self):
        for labels in ('a="1",a="2"', 'a=unquoted'):
            with self.subTest(labels=labels):
                self.assertIsNone(otel.metric(f"{NAME}{{{labels}}} 3\n", NAME))

    def test_similar_metric_names_do_not_match(self):
        self.assertIsNone(otel.metric(f"{NAME}_created 123\n", NAME))


class RetentionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.context = patch.object(otel, "OTEL_HOME", self.home)
        self.context.start()
        self.addCleanup(self.context.stop)

    def record(self, **changes):
        record = {
            "run_at": dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat(),
            "archive_root": str(self.home / "data"),
            "max_bytes": otel.ARCHIVE_MAX_BYTES,
            "max_age_days": otel.ARCHIVE_MAX_AGE_DAYS,
            "forensic_max_bytes": otel.FORENSIC_MAX_BYTES,
            "forensic_max_age_days": otel.FORENSIC_MAX_AGE_DAYS,
            "dry_run": False,
            "converged": True,
            "errors": [],
        }
        return record | changes

    def evaluate(self, record):
        return otel.retention_evidence(record, now=NOW, max_lag_seconds=900)

    def test_current_matching_record_is_valid(self):
        self.assertEqual(self.evaluate(self.record())["status"], "verified")

    def test_missing_record_is_unknown(self):
        self.assertEqual(self.evaluate(None)["status"], "unavailable")

    def test_invalid_shapes_are_not_trusted(self):
        for record in ([], "bad", True, {}, self.record(converged="false"), self.record(errors="")):
            with self.subTest(record=record):
                self.assertEqual(self.evaluate(record)["status"], "invalid")

    def test_stale_record_is_not_live_health(self):
        stamp = dt.datetime.fromtimestamp(NOW - 901, dt.timezone.utc).isoformat()
        self.assertEqual(self.evaluate(self.record(run_at=stamp))["status"], "stale")

    def test_exact_freshness_boundary(self):
        stamp = dt.datetime.fromtimestamp(NOW - 900, dt.timezone.utc).isoformat()
        self.assertEqual(self.evaluate(self.record(run_at=stamp))["status"], "verified")

    def test_bad_or_unscoped_timestamps(self):
        for stamp in (None, "not-a-time", "2033-05-18T03:33:20", dt.datetime.fromtimestamp(NOW + 1, dt.timezone.utc).isoformat()):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.evaluate(self.record(run_at=stamp))["status"], "invalid")

    def test_wrong_archive_or_policy_is_not_proof(self):
        for field, value in (("archive_root", str(self.home / "other")), ("max_bytes", 99), ("max_age_days", 1), ("forensic_max_bytes", 99), ("forensic_max_age_days", 1)):
            with self.subTest(field=field):
                self.assertEqual(self.evaluate(self.record(**{field: value}))["status"], "invalid")

    def test_dry_run_is_not_cleanup_proof(self):
        self.assertEqual(self.evaluate(self.record(dry_run=True))["status"], "invalid")

    def test_failure_and_nonconvergence_are_failed(self):
        for record in (self.record(converged=False), self.record(errors=[{"error": "private-canary"}])):
            with self.subTest(record=record):
                evidence = self.evaluate(record)
                self.assertEqual(evidence["status"], "failed")
                self.assertNotIn("private-canary", json.dumps(evidence))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "bin").mkdir()
        (self.home / "bin/control_room_otel.py").write_text("fixture")
        (self.home / "state").mkdir()
        (self.home / "data").mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(otel, "OTEL_HOME", self.home))
        self.stack.enter_context(patch.object(otel, "launchd_loaded", return_value=True))
        self.stack.enter_context(patch.object(otel, "configured", return_value=True))
        self.stack.enter_context(patch.object(otel, "archives", return_value={signal: {"malformed": 0, "records": 1, "files": 1, "items": 1, "first": None, "last": None} for signal in ("logs", "traces", "metrics")}))
        self.stack.enter_context(patch.object(otel, "prom", return_value="# no observed counters\n"))

    def write_retention(self, **changes):
        record = {
            "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "archive_root": str(self.home / "data"),
            "max_bytes": otel.ARCHIVE_MAX_BYTES,
            "max_age_days": otel.ARCHIVE_MAX_AGE_DAYS,
            "forensic_max_bytes": otel.FORENSIC_MAX_BYTES,
            "forensic_max_age_days": otel.FORENSIC_MAX_AGE_DAYS,
            "dry_run": False,
            "converged": True,
            "errors": [],
        }
        (self.home / "state/retention-last-run.json").write_text(json.dumps(record | changes))

    def test_unknown_metrics_cannot_produce_healthy(self):
        self.write_retention()
        report = otel.report()
        self.assertEqual(report["state"], "INCOMPLETE")
        self.assertIsNone(report["collector"]["export_failed_spans"])
        self.assertIn("export_failed_spans", report["unavailable_counters"])

    def test_missing_retention_is_explicit(self):
        self.assertEqual(otel.report()["retention_evidence"]["status"], "unavailable")

    def test_malformed_retention_does_not_crash_or_appear_valid(self):
        (self.home / "state/retention-last-run.json").write_text("[]")
        self.assertEqual(otel.report()["retention_evidence"]["status"], "invalid")

    def test_positive_failure_survives_other_unknown_metrics(self):
        self.write_retention()
        with patch.object(otel, "prom", return_value=f"{NAME}_total 2\n"):
            self.assertEqual(otel.report()["state"], "DEGRADED")

    def test_archive_overshoot_is_not_hidden_by_last_success(self):
        self.write_retention()
        (self.home / "data/notes.txt").write_bytes(b"12345")
        with patch.object(otel, "ARCHIVE_MAX_BYTES", 4):
            report = otel.report()
        self.assertEqual(report["state"], "DEGRADED")
        self.assertEqual(report["storage"]["overshoot_bytes"], 1)
        self.assertEqual(report["storage"]["headroom_bytes"], 0)
        self.assertTrue((self.home / "data/notes.txt").exists())

    def test_forensic_overshoot_is_not_hidden(self):
        self.write_retention()
        (self.home / "data/forensic").mkdir()
        (self.home / "data/forensic/notes.txt").write_bytes(b"12345")
        with patch.object(otel, "FORENSIC_MAX_BYTES", 4):
            report = otel.report()
        self.assertEqual(report["state"], "DEGRADED")
        self.assertEqual(report["storage"]["forensic_overshoot_bytes"], 1)

    def test_archive_budget_is_not_claimed_to_cover_auxiliary_data(self):
        self.assertEqual(otel.storage_summary()["budget_scope"], "archive_only")
        self.assertFalse(otel.storage_summary()["ancillary_bounds_verified"])

    def test_human_status_surfaces_unknowns_and_scope(self):
        self.write_retention()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            otel.human(otel.report(), status=True)
        self.assertIn("unavailable", output.getvalue().lower())
        self.assertIn("archive_only", output.getvalue())

    def test_check_exits_nonzero_for_incomplete_evidence(self):
        with patch.object(otel.sys, "argv", ["control_room_otel.py", "check", "--json"]), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            otel.main()
        self.assertEqual(result.exception.code, 1)

    def test_complete_known_evidence_can_be_healthy(self):
        self.write_retention()
        with patch.object(otel, "metric", return_value=0):
            self.assertEqual(otel.report()["state"], "HEALTHY")

    def test_stale_cleanup_blocks_healthy_with_all_counters_known(self):
        stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        self.write_retention(run_at=stamp.isoformat())
        with patch.object(otel, "metric", return_value=0):
            self.assertEqual(otel.report()["state"], "INCOMPLETE")

    def test_invalid_json_is_distinct_from_missing_record(self):
        (self.home / "state/retention-last-run.json").write_text("invalid-json")
        self.assertEqual(otel.report()["retention_evidence"]["status"], "invalid")

    def test_missing_archive_is_incomplete_not_empty_healthy(self):
        self.write_retention()
        (self.home / "data").rmdir()
        with patch.object(otel, "metric", return_value=0):
            self.assertEqual(otel.report()["state"], "INCOMPLETE")

    def test_failed_collector_takes_precedence(self):
        with patch.object(otel, "launchd_loaded", return_value=False):
            self.assertEqual(otel.report()["state"], "FAILED")

    def test_invalid_cli_lag_is_rejected(self):
        with patch.object(otel.sys, "argv", ["control_room_otel.py", "check", "--max-retention-lag-seconds", "0"]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            otel.main()
        self.assertEqual(result.exception.code, 2)

    def test_status_does_not_delete_anything(self):
        data = self.home / "data" / "historical.json"
        data.write_bytes(b"keep")
        with patch.object(Path, "unlink", side_effect=AssertionError("no deletion allowed")):
            otel.report()
        self.assertEqual(data.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
