"""Read-only profile and missing-counter regression fixtures; no live Mac access."""

import contextlib
import datetime as dt
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import control_room_otel as otel

PROFILE = {"status": "verified", "profile": "otelcol-contrib-0.159.0-file-only"}


def metrics():
    lines = []
    for wire, exporters in otel.FILE_EXPORTERS.values():
        lines += [f"otelcol_receiver_accepted_{wire} 12", f"otelcol_receiver_refused_{wire} 0"]
        lines += [f'otelcol_exporter_sent_{wire}{{exporter="{name}"}} 12' for name in exporters]
    return "\n".join(lines) + "\n"


def absent_failures():
    return {
        f"{prefix}_{suffix}": None
        for prefix in ("export_failed", "enqueue_failed")
        for suffix in otel.FILE_EXPORTERS
    }


class CounterApplicabilityTests(unittest.TestCase):
    def test_qualified_absence_is_explained_without_changing_nulls(self):
        counters = absent_failures()
        result = otel.counter_evidence(metrics(), counters, PROFILE)
        self.assertTrue(all(value is None for value in counters.values()))
        for key, item in result.items():
            self.assertEqual(
                item["status"],
                "not_applicable" if key.startswith("enqueue") else "conditionally_absent",
            )

    def test_unknown_or_different_profile_does_not_relax_checks(self):
        for profile in (
            {},
            {"status": "verified"},
            {"status": "unavailable", "profile": otel.FILE_ONLY_PROFILE},
            {"status": "verified", "profile": "new-version"},
        ):
            with self.subTest(profile=profile):
                result = otel.counter_evidence(metrics(), absent_failures(), profile)
                self.assertTrue(all(item["status"] == "unavailable" for item in result.values()))

    def test_missing_one_exporter_blocks_explanation_for_that_signal(self):
        body = "\n".join(
            line for line in metrics().splitlines() if "file/forensic_traces" not in line
        )
        result = otel.counter_evidence(body, absent_failures(), PROFILE)
        self.assertEqual(result["export_failed_spans"]["status"], "unavailable")
        self.assertEqual(result["enqueue_failed_spans"]["status"], "unavailable")
        self.assertEqual(result["export_failed_logs"]["status"], "conditionally_absent")

    def test_zero_exports_is_not_positive_activity_proof(self):
        body = metrics().replace(" 12", " 0")
        result = otel.counter_evidence(body, absent_failures(), PROFILE)
        self.assertTrue(all(item["status"] == "unavailable" for item in result.values()))

    def test_malformed_failure_is_not_an_absent_failure(self):
        for sample in ("NaN", "invalid", "-1", "0.5"):
            with self.subTest(sample=sample):
                body = metrics() + f"otelcol_exporter_send_failed_spans {sample}\n"
                self.assertEqual(
                    otel.counter_evidence(body, absent_failures(), PROFILE)["export_failed_spans"][
                        "status"
                    ],
                    "unavailable",
                )

    def test_duplicate_failure_series_is_not_ignored(self):
        body = metrics() + "otelcol_exporter_send_failed_spans 1\n" * 2
        self.assertIsNone(otel.metric(body, "otelcol_exporter_send_failed_spans"))
        self.assertEqual(
            otel.counter_evidence(body, absent_failures(), PROFILE)["export_failed_spans"][
                "status"
            ],
            "unavailable",
        )

    def test_observed_zero_and_failures_stay_observed(self):
        for value in (0, 3):
            counters = absent_failures() | {
                "export_failed_spans": value,
                "enqueue_failed_logs": value,
            }
            result = otel.counter_evidence(metrics(), counters, PROFILE)
            self.assertEqual(result["export_failed_spans"]["status"], "observed")
            self.assertEqual(result["enqueue_failed_logs"]["status"], "observed")
            self.assertEqual(counters["export_failed_spans"], value)

    def test_malformed_or_duplicate_sent_samples_are_not_activity_proof(self):
        for extra in (
            'otelcol_exporter_sent_spans{exporter="file/traces"} invalid\n',
            'otelcol_exporter_sent_spans{exporter="file/traces"} 12\n',
        ):
            result = otel.counter_evidence(metrics() + extra, absent_failures(), PROFILE)
            self.assertEqual(result["export_failed_spans"]["status"], "unavailable")

    def test_exporter_filter_matches_exact_labels(self):
        body = 'sample{exporter="file/traces"} 12\nsample{exporter="file/traces-other"} 99\n'
        self.assertEqual(otel.metric(body, "sample", exporter="file/traces"), 12)
        self.assertIsNone(otel.metric(body, "sample", exporter="missing"))

    def test_unrelated_counters_are_not_made_optional(self):
        result = otel.counter_evidence(metrics(), {"refused_spans": None}, PROFILE)
        self.assertEqual(result["refused_spans"]["status"], "unavailable")


class RuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "bin").mkdir()
        (self.home / "config").mkdir()
        self.binary = self.home / "bin/otelcol-contrib"
        self.config = self.home / "config/otelcol-macos.yaml"
        self.binary.write_text("non-executable binary fixture")
        self.config.write_text("reviewed fixture")
        self.args = [str(self.binary), "--config", str(self.config)]
        self.started = dt.datetime.now(dt.timezone.utc).timestamp() + 10
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (
            ("OTEL_HOME", self.home),
            ("FILE_ONLY_CONFIG_SHA256", hashlib.sha256(self.config.read_bytes()).hexdigest()),
        ):
            self.stack.enter_context(patch.object(otel, name, value))
        self.stack.enter_context(patch.object(otel.sys, "platform", "darwin"))
        self.stack.enter_context(
            patch.object(otel, "_collector_process", return_value=("123", self.args))
        )
        self.stack.enter_context(patch.object(otel, "_process_started", return_value=self.started))
        self.stack.enter_context(patch.object(otel, "_probe_text", return_value="123"))

    def profile(self):
        return otel.collector_counter_profile("otelcol-contrib version 0.159.0", self.started + 1)

    def test_known_runtime_and_config_pass(self):
        before = self.config.read_bytes()
        self.assertEqual(self.profile()["status"], "verified")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertNotIn(str(self.home), json.dumps(self.profile()))

    def test_version_upgrade_is_not_silently_qualified(self):
        self.assertEqual(
            otel.collector_counter_profile("otelcol-contrib version 0.160.0", self.started + 1)[
                "status"
            ],
            "unavailable",
        )

    def test_changed_config_is_unqualified(self):
        self.config.write_text("new sending_queue or exporter")
        self.assertEqual(self.profile()["reason"], "unqualified_configuration")

    def test_extra_cli_config_is_rejected(self):
        with patch.object(
            otel,
            "_collector_process",
            return_value=("123", [*self.args, "--set", "exporters=other"]),
        ):
            self.assertEqual(self.profile()["reason"], "loaded_configuration_mismatch")

    def test_config_newer_than_process_is_not_loaded_proof(self):
        with patch.object(otel, "_process_started", return_value=self.config.stat().st_ctime - 1):
            self.assertEqual(self.profile()["reason"], "files_or_process_changed_since_launch")

    def test_process_started_after_scrape_is_rejected(self):
        self.assertEqual(
            otel.collector_counter_profile("otelcol-contrib version 0.159.0", self.started - 1)[
                "reason"
            ],
            "files_or_process_changed_since_launch",
        )

    def test_other_or_multiple_listener_owners_are_rejected(self):
        for value in ("999", "123\n999", ""):
            with self.subTest(value=value), patch.object(otel, "_probe_text", return_value=value):
                self.assertEqual(self.profile()["status"], "unavailable")

    def test_process_change_during_probe_is_rejected(self):
        with patch.object(
            otel, "_collector_process", side_effect=[("123", self.args), ("124", self.args)]
        ):
            self.assertEqual(self.profile()["status"], "unavailable")

    def test_changed_process_start_is_rejected(self):
        with patch.object(otel, "_process_started", side_effect=[self.started, self.started + 1]):
            self.assertEqual(self.profile()["status"], "unavailable")

    def test_missing_file_is_rejected(self):
        self.config.unlink()
        self.assertEqual(self.profile()["status"], "unavailable")

    def test_symlink_file_is_not_accepted(self):
        target = self.config.with_suffix(".original")
        self.config.rename(target)
        self.config.symlink_to(target)
        self.assertEqual(self.profile()["status"], "unavailable")

    def test_probe_failure_does_not_leak_details(self):
        with patch.object(
            otel, "_collector_process", side_effect=subprocess.TimeoutExpired("private-canary", 3)
        ):
            result = self.profile()
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("private-canary", json.dumps(result))

    def test_no_mac_tools_on_other_platform(self):
        with (
            patch.object(otel.sys, "platform", "linux"),
            patch.object(otel, "_probe_text", side_effect=AssertionError("no tools")),
        ):
            self.assertEqual(self.profile()["reason"], "not_macos")

    def test_config_replacement_during_probe_is_rejected(self):
        def replace():
            self.config.write_text("changed during process probe")
            return "123", self.args

        with patch.object(otel, "_collector_process", side_effect=replace):
            self.assertEqual(self.profile()["status"], "unavailable")


class ProcessParsingTests(unittest.TestCase):
    def test_supervised_child_requires_exact_command_and_one_child(self):
        child = [
            str(otel.OTEL_HOME / "bin/otelcol-contrib"),
            "--config",
            str(otel.OTEL_HOME / "config/otelcol-macos.yaml"),
        ]
        wrapper = [
            "/usr/bin/python3",
            str(otel.OTEL_HOME / "bin/managed_diagnostics.py"),
            "--home",
            str(otel.OTEL_HOME),
            "--name",
            "collector",
            "--",
            *child,
        ]
        text = "pid = 123\narguments = {\n" + "\n".join(wrapper) + "\n}\n"
        with patch.object(otel, "_probe_text", side_effect=[text, "456", " ".join(child)]):
            self.assertEqual(otel._collector_process(), ("456", child))
        for outputs in ([text, "456\n789"], [text, "456", "unexpected --config"]):
            with (
                patch.object(otel, "_probe_text", side_effect=outputs),
                self.assertRaises(ValueError),
            ):
                otel._collector_process()

    def test_launchctl_argument_boundaries(self):
        text = (
            "job = {\n state = running\n pid = 123\n arguments = {\n"
            " /path with spaces/collector\n --config\n /config path\n }\n}\n"
        )
        with patch.object(otel, "_probe_text", return_value=text):
            self.assertEqual(
                otel._collector_process(),
                ("123", ["/path with spaces/collector", "--config", "/config path"]),
            )

    def test_missing_or_ambiguous_launchctl_output_fails_closed(self):
        for text in ("", "pid = 1\n", "pid = 1\npid = 2\narguments = {\na\n}\n"):
            with (
                self.subTest(text=text),
                patch.object(otel, "_probe_text", return_value=text),
                self.assertRaises(ValueError),
            ):
                otel._collector_process()

    def test_ps_start_is_parsed_as_utc(self):
        with patch.object(otel, "_probe_text", return_value="Thu Sep 10 13:00:00 2026"):
            self.assertEqual(
                otel._process_started("123"),
                dt.datetime(2026, 9, 10, 13, tzinfo=dt.timezone.utc).timestamp(),
            )

    def test_malformed_ps_output_fails_closed(self):
        with (
            patch.object(otel, "_probe_text", return_value="unknown"),
            self.assertRaises(ValueError),
        ):
            otel._process_started("123")

    def test_probe_has_timeout_and_hides_stderr(self):
        with patch.object(otel.subprocess, "check_output", return_value="123") as call:
            self.assertEqual(otel._probe_text(["ps", "-p", "123"]), "123")
        self.assertEqual(call.call_args.kwargs["timeout"], 3)
        self.assertEqual(call.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(call.call_args.kwargs["env"]["TZ"], "UTC")


class ReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "data").mkdir()
        (self.home / "state").mkdir()
        (self.home / "state/retention-last-run.json").write_text(
            json.dumps(
                {
                    "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "archive_root": str(self.home / "data"),
                    "dry_run": False,
                    "converged": True,
                    "errors": [],
                    "max_bytes": otel.ARCHIVE_MAX_BYTES,
                    "max_age_days": otel.ARCHIVE_MAX_AGE_DAYS,
                    "forensic_max_bytes": otel.FORENSIC_MAX_BYTES,
                    "forensic_max_age_days": otel.FORENSIC_MAX_AGE_DAYS,
                }
            )
        )
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(otel, "OTEL_HOME", self.home))
        self.stack.enter_context(patch.object(otel, "launchd_loaded", return_value=True))
        self.stack.enter_context(patch.object(otel, "configured", return_value=True))
        self.stack.enter_context(
            patch.object(otel, "collector_counter_profile", return_value=PROFILE)
        )
        self.stack.enter_context(patch.object(otel, "prom", return_value=metrics()))
        self.stack.enter_context(
            patch.object(
                otel,
                "archives",
                return_value={
                    signal: {
                        "malformed": 0,
                        "records": 1,
                        "files": 1,
                        "items": 1,
                        "first": None,
                        "last": None,
                    }
                    for signal in ("logs", "traces", "metrics")
                },
            )
        )

    def test_qualified_absence_is_not_incomplete(self):
        report = otel.report()
        self.assertEqual(report["state"], "HEALTHY")
        self.assertEqual(len(report["unavailable_counters"]), 6)
        self.assertEqual(report["unexplained_counters"], [])
        self.assertTrue(
            all(report["collector"][key] is None for key in report["unavailable_counters"])
        )
        self.assertFalse(report["storage"]["ancillary_bounds_verified"])

    def test_unqualified_runtime_stays_incomplete(self):
        with patch.object(otel, "collector_counter_profile", return_value={}):
            self.assertEqual(otel.report()["state"], "INCOMPLETE")

    def test_export_failure_remains_degraded(self):
        body = metrics() + 'otelcol_exporter_send_failed_spans{exporter="file/traces"} 2\n'
        with patch.object(otel, "prom", return_value=body):
            self.assertEqual(otel.report()["state"], "DEGRADED")

    def test_enqueue_failure_is_not_hidden_by_no_queue_rule(self):
        body = metrics() + "otelcol_exporter_enqueue_failed_spans 2\n"
        with patch.object(otel, "prom", return_value=body):
            self.assertEqual(otel.report()["state"], "DEGRADED")

    def test_malformed_failure_stays_incomplete(self):
        with patch.object(
            otel, "prom", return_value=metrics() + "otelcol_exporter_send_failed_spans NaN\n"
        ):
            self.assertEqual(otel.report()["state"], "INCOMPLETE")

    def test_missing_required_counter_stays_incomplete(self):
        body = "\n".join(
            line
            for line in metrics().splitlines()
            if not line.startswith("otelcol_receiver_refused_spans")
        )
        with patch.object(otel, "prom", return_value=body):
            self.assertEqual(otel.report()["state"], "INCOMPLETE")

    def test_overshoot_is_reported_without_failing_capture(self):
        (self.home / "data/notes").write_bytes(b"12345")
        with patch.object(otel, "ARCHIVE_MAX_BYTES", 4):
            result = otel.report()
        self.assertEqual(result["state"], "INCOMPLETE")
        self.assertTrue(result["storage_target_exceeded"])

    def test_stale_retention_stays_incomplete(self):
        with patch.object(otel, "retention_evidence", return_value={"status": "stale"}):
            self.assertEqual(otel.report()["state"], "INCOMPLETE")

    def test_down_collector_remains_failed(self):
        with patch.object(otel, "launchd_loaded", return_value=False):
            self.assertEqual(otel.report()["state"], "FAILED")

    def test_check_passes_only_for_qualified_evidence(self):
        with (
            patch.object(otel.sys, "argv", ["helper", "check", "--json"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            otel.main()
        with (
            patch.object(otel, "collector_counter_profile", return_value={}),
            patch.object(otel.sys, "argv", ["helper", "check", "--json"]),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            otel.main()
        self.assertEqual(raised.exception.code, 1)

    def test_human_status_explains_without_claiming_measured_zero(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            otel.human(otel.report())
        self.assertIn("not_applicable", output.getvalue())
        self.assertIn("conditionally_absent", output.getvalue())
        self.assertIn("raw value unavailable", output.getvalue())


if __name__ == "__main__":
    unittest.main()
