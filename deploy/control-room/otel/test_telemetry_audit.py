#!/usr/bin/env python3
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("telemetry_audit.py")
SPEC = importlib.util.spec_from_file_location("telemetry_audit", MODULE_PATH)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def kv(key, value):
    if isinstance(value, bool):
        kind = "boolValue"
    elif isinstance(value, int):
        kind = "intValue"
    else:
        kind = "stringValue"
    return {"key": key, "value": {kind: value}}


class TelemetryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "lean"
        for signal in AUDIT.SIGNALS:
            (self.root / signal).mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, *requests, raw_lines=()):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as handle:
            for request in requests:
                handle.write(json.dumps(request) + "\n")
            for line in raw_lines:
                handle.write(line + "\n")
        return path

    def resource(self):
        return {
            "attributes": [
                kv("service.name", "codex-app-server"),
                kv("service.version", "1.2.3"),
                kv("controlroom.capture_node_id", "private-node-id"),
                kv("controlroom.source.role", "mac-codex"),
            ]
        }

    def logs(self):
        record = {
            "traceId": "trace-a",
            "spanId": "span-log",
            "timeUnixNano": "1000000000",
            "attributes": [
                kv("conversation.id", "conversation-a"),
                kv("model", "gpt-test"),
                kv("provider_name", "openai"),
                kv("reasoning_effort", "high"),
                kv("input_token_count", 10),
                kv("tool_name", "shell"),
                kv("success", True),
                kv("duration_ms", 5),
                kv("sandbox_policy", "workspace"),
                kv("app.version", "26.1"),
                kv("prompt", "[REDACTED]"),
            ],
        }
        missing = {"timeUnixNano": "2000000000", "attributes": []}
        return {
            "resourceLogs": [
                {
                    "resource": self.resource(),
                    "scopeLogs": [{"scope": {"name": "test"}, "logRecords": [record, missing]}],
                }
            ]
        }

    def traces(self, cwd=True):
        parent_attrs = [
            kv("conversation.id", "conversation-a"),
            kv("session.id", "session-a"),
            kv("turn.id", "turn-a"),
            kv("model", "gpt-test"),
            kv("provider", "openai"),
            kv("codex.request.reasoning_effort", "high"),
        ]
        if cwd:
            parent_attrs.append(kv("cwd", "/Users/private/work/repo-one"))
        spans = [
            {
                "traceId": "trace-a",
                "spanId": "span-parent",
                "name": "turn",
                "startTimeUnixNano": "1000000000",
                "endTimeUnixNano": "3000000000",
                "status": {"code": 1},
                "attributes": parent_attrs,
            },
            {
                "traceId": "trace-a",
                "spanId": "span-child",
                "parentSpanId": "span-parent",
                "name": "tool",
                "startTimeUnixNano": "1500000000",
                "endTimeUnixNano": "2500000000",
                "attributes": [kv("tool.name", "exec_command"), kv("success", True)],
            },
            {
                "traceId": "trace-a",
                "spanId": "span-orphan",
                "parentSpanId": "missing-parent",
                "name": "orphan",
                "startTimeUnixNano": "2000000000",
                "endTimeUnixNano": "2000000001",
                "status": {"code": 2},
                "attributes": [kv("error.type", "test-error")],
            },
        ]
        return {
            "resourceSpans": [
                {
                    "resource": self.resource(),
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": spans}],
                }
            ]
        }

    def metrics(self):
        metrics = [
            {
                "name": "codex.turn.token_usage",
                "gauge": {
                    "dataPoints": [
                        {
                            "timeUnixNano": "3000000000",
                            "attributes": [
                                kv("conversation.id", "conversation-a"),
                                kv("turn.id", "turn-a"),
                            ],
                            "exemplars": [
                                {"traceId": "trace-a", "spanId": "span-child", "asInt": "1"}
                            ],
                            "asInt": "10",
                        }
                    ]
                },
            }
        ]
        return {
            "resourceMetrics": [
                {
                    "resource": self.resource(),
                    "scopeMetrics": [{"scope": {"name": "test"}, "metrics": metrics}],
                }
            ]
        }

    def populate(self, cwd=True):
        self.write("logs/logs.otlp-old-size.json", self.logs())
        self.write("traces/traces.otlp.json", self.traces(cwd=cwd))
        self.write("metrics/metrics.otlp.json", self.metrics())

    def test_parses_rotated_logs_traces_metrics_and_completeness(self):
        self.populate()
        report = AUDIT.Audit(self.root).run()
        self.assertEqual(report["signals"]["logs"]["files"], 1)
        self.assertEqual(report["signals"]["logs"]["items"], 2)
        self.assertEqual(report["signals"]["traces"]["items"], 3)
        self.assertEqual(report["signals"]["metrics"]["items"], 1)
        self.assertEqual(
            report["fields"]["conversation_id"]["by_signal"]["logs"]["completeness_percent"], 50.0
        )
        self.assertEqual(
            report["safe_categorical_values"]["reported_model"]["counts"], {"gpt-test": 2}
        )
        self.assertIn("codex.turn.token_usage", report["schema"]["metrics"]["metric_names"])

    def test_conversation_turn_trace_correlation_and_parent_integrity(self):
        self.populate()
        report = AUDIT.Audit(self.root).run()
        self.assertEqual(report["identity"]["unique_conversations"], 1)
        self.assertEqual(report["identity"]["unique_turns"], 1)
        self.assertEqual(
            report["correlation"]["trace"]["unique_by_signal"],
            {"logs": 1, "traces": 1, "metrics": 1},
        )
        self.assertEqual(report["correlation"]["logs_to_spans"]["coverage_percent"], 100.0)
        self.assertEqual(report["parent_integrity"]["parents_present_in_lean_archive"], 1)
        self.assertEqual(report["parent_integrity"]["orphan_parent_references"], 1)
        self.assertEqual(report["tools"]["items_with_tool_identity"], 2)
        self.assertEqual(report["tools"]["tool_outcome_coverage_percent"], 100.0)
        linked = report["repository_project_attribution"]["trace_linked_identity_attribution"]
        self.assertEqual(linked["conversations"]["with_one_context"], 1)
        self.assertEqual(linked["turns"]["with_one_context"], 1)

    def test_malformed_missing_optional_fields_and_runtime(self):
        self.write("logs/logs.otlp.json", self.logs(), raw_lines=("{partial",))
        self.write("traces/traces.otlp.json", self.traces())
        report = AUDIT.Audit(self.root).run()
        self.assertEqual(report["signals"]["logs"]["malformed_records"], 1)
        self.assertEqual(report["files"]["malformed_records"][0]["line"], 2)
        self.assertEqual(report["runtime"]["spans_with_calculable_duration"], 3)
        self.assertEqual(report["fields"]["requested_model"]["status"], "not_observed")
        self.assertIn("requested_model", report["fields_searched_but_not_observed"])

    def test_privacy_safe_rendering_and_workspace_attribution(self):
        self.populate()
        report = AUDIT.Audit(self.root).run()
        rendered = json.dumps(report)
        self.assertNotIn("/Users/private", rendered)
        self.assertNotIn("private-node-id", rendered)
        self.assertNotIn("exec_command", rendered)
        self.assertEqual(report["privacy"]["prompt_redaction_marker_occurrences"], 1)
        self.assertEqual(report["privacy"]["non_redaction_prompt_values_observed"], 0)
        attribution = report["repository_project_attribution"]
        self.assertEqual(attribution["evidence"], "workspace_only")
        self.assertEqual(attribution["unique_hashed_contexts"]["workspace"], 1)
        self.assertFalse(attribution["raw_paths_or_identifiers_emitted"])

    def test_clean_attribution_absence(self):
        self.populate(cwd=False)
        report = AUDIT.Audit(self.root).run()
        attribution = report["repository_project_attribution"]
        self.assertEqual(attribution["evidence"], "absent")
        self.assertEqual(attribution["coverage_percent"], 0.0)

    def test_start_time_filter_is_read_only_and_reports_skipped_items(self):
        self.populate()
        report = AUDIT.Audit(self.root, start_time_ns=2_000_000_000).run()
        self.assertEqual(report["signals"]["logs"]["items"], 1)
        self.assertEqual(report["signals"]["traces"]["items"], 1)
        self.assertEqual(report["signals"]["metrics"]["items"], 1)
        self.assertEqual(
            report["time_filter"]["skipped_before_start"],
            {"logs": 1, "traces": 2, "metrics": 0},
        )
        self.assertEqual(
            report["time_filter"]["skipped_missing_timestamp"],
            {"logs": 0, "traces": 0, "metrics": 0},
        )
        self.assertEqual(report["privacy"]["prompt_redaction_marker_occurrences"], 0)

    def test_cli_json_and_human_outputs(self):
        self.populate()
        json_run = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--root", str(self.root), "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(json_run.stdout)["mode"], "read_only_lean_archive")
        filtered_run = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--root",
                str(self.root),
                "--json",
                "--start-time",
                "1970-01-01T00:00:02Z",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(filtered_run.stdout)["signals"]["traces"]["items"], 1)
        human_run = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--root", str(self.root)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Privacy: no raw payload values emitted", human_run.stdout)
        self.assertNotIn("/Users/private", human_run.stdout)
        otel_home = Path(self.temp.name) / "otel-home"
        moved = otel_home / "data/lean"
        moved.parent.mkdir(parents=True)
        self.root.rename(moved)
        self.assertEqual(AUDIT._lean_root(otel_home), moved)


if __name__ == "__main__":
    unittest.main()
