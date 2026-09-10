"""Offline regression fixtures; these never start Codex or a Collector."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import preflight_codex_config as checker

ORIGIN = "http://127.0.0.1:4318"


def config_text(origin: str = ORIGIN) -> str:
    lines = ["[otel]", "log_user_prompt = false"]
    for signal, field in checker.SIGNALS.items():
        lines.append(
            f'{field} = {{ otlp-http = {{ endpoint = "{origin}/v1/{signal}", protocol = "binary" }} }}'
        )
    return "\n".join(lines) + "\n"


class ConfigPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.desktop = self.root / "desktop.toml"
        self.omnigent = self.root / "private.toml"
        self.desktop.write_text(config_text())
        self.omnigent.write_text(config_text())

    def report(self) -> dict:
        return checker.preflight(self.desktop, self.omnigent, ORIGIN)

    def test_matching_files_do_not_claim_runtime_proof(self) -> None:
        report = self.report()
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["runtime_verified"])
        self.assertEqual(report["scope"], "supplied_config_files_only")

    def test_reads_leave_original_bytes_unchanged(self) -> None:
        before = self.desktop.read_bytes(), self.omnigent.read_bytes()
        self.report()
        self.assertEqual(before, (self.desktop.read_bytes(), self.omnigent.read_bytes()))

    def test_missing_otel_copy_fails(self) -> None:
        self.omnigent.write_text('model = "fixture"\n')
        self.assertIn("missing_otel_table", self.report()["producers"]["omnigent_codex"]["issues"])

    def test_missing_file_fails(self) -> None:
        report = checker.preflight(self.root / "missing", self.omnigent, ORIGIN)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["producers"]["desktop"]["read_status"], "missing")

    def test_privacy_must_be_explicit_false(self) -> None:
        for replacement in ("true", '"false"', "0"):
            with self.subTest(replacement=replacement):
                self.omnigent.write_text(config_text().replace("false", replacement, 1))
                self.assertIn("explicit_prompt_privacy_required", self.report()["producers"]["omnigent_codex"]["issues"])
        self.omnigent.write_text(config_text().replace("log_user_prompt = false\n", ""))
        self.assertEqual(self.report()["status"], "fail")

    def test_all_three_signals_are_checked(self) -> None:
        for signal in checker.SIGNALS:
            with self.subTest(signal=signal):
                self.omnigent.write_text(config_text().replace(f"/v1/{signal}", "/v1/wrong"))
                report = self.report()
                self.assertEqual(report["status"], "fail")
                self.assertFalse(report["producers"]["omnigent_codex"]["signals"][signal]["matches"])

    def test_each_producer_must_match_expected_collector(self) -> None:
        self.desktop.write_text(config_text("http://127.0.0.1:9999"))
        self.omnigent.write_text(self.desktop.read_text())
        self.assertEqual(self.report()["status"], "fail")

    def test_different_environment_tags_do_not_change_destination(self) -> None:
        self.omnigent.write_text(config_text() + 'environment = "omnigent-codex"\n')
        self.assertEqual(self.report()["status"], "pass")

    def test_json_http_protocol_is_supported(self) -> None:
        self.omnigent.write_text(config_text().replace('"binary"', '"json"'))
        self.assertEqual(self.report()["status"], "pass")

    def test_unsupported_protocol_is_not_assumed(self) -> None:
        self.omnigent.write_text(config_text().replace('"binary"', '"unsupported"'))
        self.assertEqual(self.report()["status"], "fail")

    def test_non_string_protocol_is_reported_not_raised(self) -> None:
        self.omnigent.write_text(config_text().replace('"binary"', '["binary"]'))
        self.assertEqual(self.report()["status"], "fail")

    def test_grpc_is_outside_this_http_preflight(self) -> None:
        self.omnigent.write_text(config_text().replace("otlp-http", "otlp-grpc"))
        self.assertEqual(self.report()["status"], "fail")

    def test_disabled_exporter_fails(self) -> None:
        text = config_text().splitlines()
        self.omnigent.write_text("\n".join([*text[:2], 'exporter = "none"', *text[3:]]))
        self.assertEqual(self.report()["status"], "fail")

    def test_private_payloads_and_paths_never_appear_in_report(self) -> None:
        self.omnigent.write_text(config_text() + '\n[other]\nsecret = "sensitive-canary"\n')
        text = json.dumps(self.report())
        self.assertNotIn("sensitive-canary", text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(ORIGIN, text)

    def test_invalid_toml_does_not_echo_parser_context(self) -> None:
        self.omnigent.write_text('sensitive-canary = "unterminated')
        report = self.report()
        self.assertEqual(report["producers"]["omnigent_codex"]["read_status"], "invalid_toml")
        self.assertNotIn("sensitive-canary", json.dumps(report))

    def test_endpoint_credentials_query_and_fragment_are_rejected_without_echo(self) -> None:
        for value in (
            "http://username:sensitive-canary@127.0.0.1:4318/v1/traces",
            "http://127.0.0.1:4318/v1/traces?token=sensitive-canary",
            "http://127.0.0.1:4318/v1/traces#sensitive-canary",
        ):
            with self.subTest(value=value):
                self.omnigent.write_text(config_text().replace(ORIGIN + "/v1/traces", value))
                self.assertEqual(self.report()["status"], "fail")
                self.assertNotIn("sensitive-canary", json.dumps(self.report()))

    def test_auth_headers_never_appear_in_report(self) -> None:
        self.omnigent.write_text(config_text().replace('protocol = "binary"', 'protocol = "binary", headers = { Authorization = "sensitive-canary" }'))
        self.assertEqual(self.report()["status"], "pass")
        self.assertNotIn("sensitive-canary", json.dumps(self.report()))

    def test_origin_validation_never_resolves_remote_hosts(self) -> None:
        for value in ("http://example.org:4318", "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:4318/path", "http://u:secret@127.0.0.1:4318", "http://127.0.0.1:4318?secret", "file:///secret", "http://[invalid]:4318"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checker.local_origin(value)

    def test_ipv6_and_localhost_supported_without_equating_distinct_hosts(self) -> None:
        self.assertEqual(checker.local_origin("http://[::1]:4318"), ("http", "::1", 4318))
        self.assertEqual(checker.local_origin("http://localhost:4318"), ("http", "localhost", 4318))
        self.assertFalse(checker.endpoint_matches("http://127.0.0.2:4318/v1/logs", checker.local_origin(ORIGIN), "logs"))

    def test_large_file_is_not_read(self) -> None:
        with patch.object(checker, "MAX_CONFIG_BYTES", 4):
            self.assertEqual(self.report()["producers"]["desktop"]["read_status"], "config_too_large")

    def test_fifo_does_not_block(self) -> None:
        path = self.root / "fifo"
        os.mkfifo(path)
        self.assertEqual(checker.read_config(path)[1], "not_regular_file")

    def test_cli_returns_exit_codes_and_json(self) -> None:
        args = ["--desktop-config", str(self.desktop), "--omnigent-config", str(self.omnigent), "--collector-origin", ORIGIN]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(checker.main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "pass")
        self.omnigent.write_text("")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checker.main(args), 1)

    def test_invalid_origin_cli_does_not_echo_value(self) -> None:
        output = io.StringIO()
        args = ["--desktop-config", str(self.desktop), "--omnigent-config", str(self.omnigent), "--collector-origin", "http://sensitive-canary.example:4318"]
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as exit_context:
            checker.main(args)
        self.assertEqual(exit_context.exception.code, 2)
        self.assertNotIn("sensitive-canary", output.getvalue())


if __name__ == "__main__":
    unittest.main()
