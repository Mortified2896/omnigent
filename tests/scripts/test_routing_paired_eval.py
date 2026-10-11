import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.routing_paired_eval import (
    bounded_process,
    cli_metrics,
    copy_source,
    junit_result,
    native_command,
)


def test_submission_does_not_import_model_modified_tests_or_symlinks(tmp_path):
    source, grading = tmp_path / "submission", tmp_path / "grading"
    source.mkdir()
    grading.mkdir()
    (source / "module.py").write_text("fixed source")
    (source / "test.py").write_text("do not grade this")
    copy_source(source, grading, ["module.py"])
    assert (grading / "module.py").read_text() == "fixed source"
    assert not (grading / "test.py").exists()
    (source / "linked.py").symlink_to(source / "module.py")
    with pytest.raises(ValueError, match="Symlink"):
        copy_source(source, grading, ["linked.py"])
    with pytest.raises(ValueError, match="relative path"):
        copy_source(source, grading, ["../outside.py"])


def test_acceptance_requires_executed_cases_not_skips_or_an_empty_run(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text(
        "<testsuites><testsuite><testcase><skipped/></testcase></testsuite></testsuites>"
    )
    assert junit_result(report, 1)["passed"] is False
    report.write_text("<testsuites><testsuite><testcase/><testcase/></testsuite></testsuites>")
    assert junit_result(report, 2)["passed"] is True
    report.write_text(
        "<testsuites><testsuite><testcase><failure/></testcase></testsuite></testsuites>"
    )
    assert junit_result(report, 1)["passed"] is False


def test_runtime_receipts_keep_requested_attribution_distinct_from_usage(tmp_path):
    receipt = tmp_path / "events.jsonl"
    receipt.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"type": "item.completed", "item": {"type": "command_execution"}},
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2},
                },
            ]
        )
    )
    metrics = cli_metrics(receipt)
    assert metrics["completed_turns"] == 1
    assert metrics["tool_or_file_events"] == 1
    assert metrics["usage"]["cached_input_tokens"] == 4
    assert metrics["actual_model_from_usage"] is None
    assert metrics["spending"] is None
    command = native_command(
        "codex",
        {"model": "fixture-model", "effort": "fixture-effort"},
        Path("/fixture"),
        Path("/fixture/answer"),
    )
    assert "--ephemeral" in command
    assert 'approval_policy="never"' in command
    assert "workspace-write" in command


@pytest.mark.skipif(os.name != "posix", reason="Owned process groups require POSIX")
def test_timeout_stops_a_tool_child_even_when_its_cli_parent_exits(tmp_path):
    pid_file = tmp_path / "child.pid"
    child = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    parent = (
        "import subprocess,sys,time,pathlib;"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));time.sleep(60)"
    )
    with (tmp_path / "output").open("wb") as output:
        _, timed_out = bounded_process(
            [sys.executable, "-c", parent],
            timeout=0.4,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    assert timed_out is True
    child_pid = int(pid_file.read_text())
    # A killed orphan may briefly remain a zombie until the host reaps it.
    for _ in range(20):
        status = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not status or status.startswith("Z"):
            break
        time.sleep(0.01)
    assert not status or status.startswith("Z")
