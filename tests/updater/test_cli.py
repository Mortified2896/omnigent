"""CLI smoke tests for ``omnigent-updater``."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.updater import cli


def test_cli_record_writes_request_and_returns_id(state_root: Path, capsys) -> None:
    rc = cli.main(
        [
            "record",
            "--target-sha",
            "0" * 40,
            "--expected-current-sha",
            "0" * 40,
            "--operator",
            "tester",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["request_id"]
    request_path = Path(payload["request_path"])
    assert request_path.is_file()


def test_cli_status_prints_request_payload(state_root: Path, capsys) -> None:
    rec_rc = cli.main(
        [
            "record",
            "--target-sha",
            "0" * 40,
            "--expected-current-sha",
            "0" * 40,
            "--operator",
            "tester",
        ]
    )
    assert rec_rc == 0
    rec_out = capsys.readouterr().out
    request_id = json.loads(rec_out)["request_id"]
    rc = cli.main(["status", request_id])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["request"]["request_id"] == request_id


def test_cli_list_includes_recorded_request(state_root: Path, capsys) -> None:
    cli.main(
        [
            "record",
            "--target-sha",
            "0" * 40,
            "--expected-current-sha",
            "0" * 40,
            "--operator",
            "tester",
        ]
    )
    capsys.readouterr()
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "in-flight" in out


def test_cli_recover_runs(state_root: Path, capsys) -> None:
    rc = cli.main(["recover"])
    assert rc == 0
    out = capsys.readouterr().out
    # Either an empty list or a list of decisions; both are valid
    # JSON arrays.
    payload = json.loads(out)
    assert isinstance(payload, list)


def test_cli_show_result_missing_returns_error(state_root: Path, capsys) -> None:
    rc = cli.main(["show-result", "ZZZZZZZZZZZZZZZZZZZZZZZZ"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no result" in err


def test_cli_record_rejects_malformed_sha(state_root: Path, capsys) -> None:
    rc = cli.main(
        [
            "record",
            "--target-sha",
            "not-a-sha",
            "--expected-current-sha",
            "0" * 40,
            "--operator",
            "tester",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "40-character" in err or "lowercase hex" in err
