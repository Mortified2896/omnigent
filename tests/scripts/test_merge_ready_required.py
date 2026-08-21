"""Offline contract tests for the fork's Merge Ready required-check set."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SH = REPO_ROOT / ".github/scripts/merge-ready/required.sh"
EVALUATE_SH = REPO_ROOT / ".github/scripts/merge-ready/evaluate-checks.sh"
COMMENT_INTENT_SH = REPO_ROOT / ".github/scripts/merge-ready/enable-automerge-comment.sh"
LABEL_INTENT_SH = REPO_ROOT / ".github/scripts/merge-ready/enable-automerge-label.sh"
WORKFLOWS = REPO_ROOT / ".github/workflows"

DEDICATED_JOBS: dict[str, tuple[str, ...]] = {
    "lint.yml": ("pre-commit", "version-lockstep"),
    "security-scan.yml": ("scan",),
    "docker-build.yml": ("build",),
    "web-tests.yml": ("web-test",),
    "ui-snapshot.yml": ("ui-snapshot",),
}
NON_SKIPPABLE_JOBS: dict[str, tuple[str, ...]] = {
    "lint.yml": ("pre-commit", "version-lockstep"),
    "security-scan.yml": ("scan",),
}


def _workflow(filename: str) -> dict[str, Any]:
    data = yaml.load((WORKFLOWS / filename).read_text(), Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    return data


def _bash_array(name: str) -> list[str]:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; declare -n values="$2"; printf "%s\\n" "${values[@]}"',
            "bash",
            str(REQUIRED_SH),
            name,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.splitlines()


def _allow_skip_mappings() -> dict[str, str]:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; for check in "${ALLOW_SKIP[@]}"; do '
                'printf "%s\\t%s\\n" "$check" "$(workflow_for "$check")"; done'
            ),
            "bash",
            str(REQUIRED_SH),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return dict(line.split("\t", 1) for line in proc.stdout.splitlines())


def _required_workflow_mappings() -> dict[str, str]:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; for check in "${REQUIRED[@]}"; do '
                'printf "%s\\t%s\\n" "$check" "$(workflow_for "$check")"; done'
            ),
            "bash",
            str(REQUIRED_SH),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return dict(line.split("\t", 1) for line in proc.stdout.splitlines())


def _run_evaluate(
    tmp_path: Path,
    *,
    checks: list[str],
    workflow_runs: list[str],
    attempt_jobs: dict[tuple[str, str], list[str] | None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    checks_file = tmp_path / "checks"
    workflow_runs_file = tmp_path / "workflow-runs"
    attempt_jobs_file = tmp_path / "attempt-jobs"
    output_file = tmp_path / "evaluate-output"
    fake_gh = tmp_path / "gh"
    checks_file.write_text("\n".join(checks) + "\n")
    normalized_runs = [row if len(row.split("\t")) == 7 else f"{row}\t1" for row in workflow_runs]
    workflow_runs_file.write_text("\n".join(normalized_runs) + "\n")
    jobs: dict[tuple[str, str], list[str] | None] = {}
    for run in normalized_runs:
        fields = run.split("\t")
        assert len(fields) == 7
        suite, run_id, attempt = fields[4:7]
        jobs[(run_id, attempt)] = [
            check_fields[5]
            for check in checks
            if len(check_fields := check.split("\t")) == 6 and check_fields[4] == suite
        ]
    jobs.update(attempt_jobs or {})
    attempt_job_rows = []
    for (run_id, attempt), ids in jobs.items():
        if ids is None:
            attempt_job_rows.append(f"{run_id}\t{attempt}\t__API_ERROR__")
        else:
            attempt_job_rows.extend(f"{run_id}\t{attempt}\t{job_id}" for job_id in ids)
    attempt_jobs_file.write_text("\n".join(attempt_job_rows) + "\n")
    output_file.write_text("")
    fake_gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$2" == *"/check-runs?filter=all&per_page=100" ]]; then
  cat "$FAKE_CHECKS_FILE"
elif [[ "$2" =~ /actions/runs/([0-9]+)/attempts/([0-9]+)/jobs\\?per_page=100 ]]; then
  RUN_ID="${BASH_REMATCH[1]}"
  ATTEMPT="${BASH_REMATCH[2]}"
  if awk -F '\t' -v r="$RUN_ID" -v a="$ATTEMPT" \
    '$1 == r && $2 == a && $3 == "__API_ERROR__" {found=1} END {exit !found}' \
    "$FAKE_ATTEMPT_JOBS_FILE"; then
    exit 1
  fi
  awk -F '\t' -v r="$RUN_ID" -v a="$ATTEMPT" \
    '$1 == r && $2 == a {print $3}' "$FAKE_ATTEMPT_JOBS_FILE"
elif [[ "$2" == *actions/runs* ]]; then
  cat "$FAKE_WORKFLOW_RUNS_FILE"
else
  exit 2
fi
"""
    )
    fake_gh.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "FAKE_CHECKS_FILE": str(checks_file),
            "FAKE_ATTEMPT_JOBS_FILE": str(attempt_jobs_file),
            "FAKE_WORKFLOW_RUNS_FILE": str(workflow_runs_file),
            "GH_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(output_file),
            "PATH": f"{tmp_path}:{env['PATH']}",
            "REPO": "example/omnigent",
            "SHA": "0123456789abcdef",
        }
    )
    proc = subprocess.run(
        ["bash", str(EVALUATE_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc, output_file.read_text()


def _successful_evaluator_fixture(
    base: int,
) -> tuple[
    list[str],
    list[str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    mappings = _required_workflow_mappings()
    required = _bash_array("REQUIRED")
    producers = sorted(set(mappings.values()))
    suites = {producer: str(base + 1000 + index) for index, producer in enumerate(producers)}
    run_ids = {producer: str(base + 2000 + index) for index, producer in enumerate(producers)}
    check_ids = {check: str(base + index) for index, check in enumerate(required)}
    checks = [
        (
            f"{check}\tcompleted\tsuccess\t2026-08-20T02:00:00Z"
            f"\t{suites[mappings[check]]}\t{check_ids[check]}"
        )
        for check in required
    ]
    workflow_runs = [
        (
            f"{producer}\tcompleted\tsuccess\t2026-08-20T02:{index:02d}:00Z"
            f"\t{suites[producer]}\t{run_ids[producer]}\t1"
        )
        for index, producer in enumerate(producers)
    ]
    return checks, workflow_runs, check_ids, run_ids, suites, mappings


def _jobs(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    assert all(isinstance(job_id, str) and isinstance(job, dict) for job_id, job in jobs.items())
    return jobs


def _job_name(workflow: dict[str, Any], job_id: str) -> str:
    name = _jobs(workflow)[job_id].get("name")
    assert isinstance(name, str)
    return name


def _workflow_name(workflow: dict[str, Any]) -> str:
    name = workflow.get("name")
    assert isinstance(name, str)
    return name


def _workflow_step(
    workflow: dict[str, Any], name: str, *, job_id: str = "evaluate"
) -> dict[str, Any]:
    steps = _jobs(workflow)[job_id].get("steps")
    assert isinstance(steps, list)
    matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _compact_condition(step: dict[str, Any]) -> str:
    condition = step.get("if")
    assert isinstance(condition, str)
    return " ".join(condition.split())


def _run_status_step(
    step: dict[str, Any],
    *,
    tmp_path: Path,
    log_file: Path,
    state: str | None = None,
    description: str | None = None,
    action: str = "completed",
    event_name: str = "workflow_run",
    source_status: str = "completed",
) -> dict[str, str]:
    script = step.get("run")
    assert isinstance(script, str)
    step_env = step.get("env")
    assert isinstance(step_env, dict)

    output_file = tmp_path / "status-step-output"
    output_file.write_text("")

    env = os.environ.copy()
    env.update(
        {
            key: value
            for key, value in step_env.items()
            if isinstance(key, str) and isinstance(value, str) and "${{" not in value
        }
    )
    env.update(
        {
            "FAKE_GH_LOG": str(log_file),
            "FAKE_SOURCE_STATUS": source_status,
            "EVENT_NAME": event_name,
            "GH_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(output_file),
            "PATH": f"{tmp_path}:{env['PATH']}",
            "REPO": "example/omnigent",
            "RUN_ID": "123456",
            "SHA": "0123456789abcdef0123456789abcdef01234567",
            "ACTION": action,
        }
    )
    if state is not None:
        env["STATE"] = state
    if description is not None:
        env["DESC"] = description

    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return dict(line.split("=", 1) for line in output_file.read_text().splitlines() if "=" in line)


def _run_inline_step(
    step: dict[str, Any],
    *,
    tmp_path: Path,
    env_update: dict[str, str],
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    script = step.get("run")
    assert isinstance(script, str)
    output_file = tmp_path / "step-output"
    output_file.write_text("")
    env = os.environ.copy()
    env.update(
        {
            "GH_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(output_file),
            "PATH": f"{tmp_path}:{env['PATH']}",
            **env_update,
        }
    )
    proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    outputs = dict(
        line.split("=", 1) for line in output_file.read_text().splitlines() if "=" in line
    )
    return proc, outputs


def _status_call(call: list[str]) -> tuple[str, dict[str, str]]:
    assert call[:3] == ["api", "--method", "POST"]
    endpoint = call[3]
    fields: dict[str, str] = {}
    index = 4
    while index < len(call):
        assert call[index] == "-f"
        key, separator, value = call[index + 1].partition("=")
        assert separator == "="
        fields[key] = value
        index += 2
    return endpoint, fields


def _selected_job_producers(
    selections: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    """Select policy-owned job ids and derive check-to-workflow mappings."""
    result: dict[str, str] = {}
    for filename, job_ids in selections.items():
        workflow = _workflow(filename)
        producer = _workflow_name(workflow)
        for job_id in job_ids:
            check = _job_name(workflow, job_id)
            assert check not in result
            result[check] = producer
    return result


def _ci_checks() -> set[str]:
    workflow = _workflow("ci.yml")
    jobs = _jobs(workflow)
    matrix_job = next(
        job for job in jobs.values() if job.get("name") == "Pytest (${{ matrix.group }})"
    )
    strategy = matrix_job.get("strategy")
    assert isinstance(strategy, dict)
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict)
    includes = matrix.get("include")
    assert isinstance(includes, list)
    groups = {entry["group"] for entry in includes if isinstance(entry, dict)}
    assert all(isinstance(group, str) for group in groups)

    checks = {f"Pytest ({group})" for group in groups}
    checks.update(
        name
        for job in jobs.values()
        if isinstance((name := job.get("name")), str)
        and name.startswith("Pytest (")
        and "${{" not in name
    )
    return checks


def _shard_checks(filename: str) -> set[str]:
    workflow = _workflow(filename)
    jobs = _jobs(workflow)
    template = next(
        name
        for job in jobs.values()
        if isinstance((name := job.get("name")), str) and "matrix.shard_id" in name
    )
    shard_counts: set[int] = set()
    for job in jobs.values():
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict) or not isinstance((env := step.get("env")), dict):
                continue
            raw_count = env.get("NUM_SHARDS")
            if isinstance(raw_count, str) and raw_count.isdecimal():
                shard_counts.add(int(raw_count))
    assert len(shard_counts) == 1
    shard_count = shard_counts.pop()
    return {
        template.replace("${{ matrix.shard_id }}", str(shard_id)).replace(
            "${{ matrix.num_shards }}", str(shard_count)
        )
        for shard_id in range(shard_count)
    }


def _integration_checks(tmp_path: Path) -> set[str]:
    output = tmp_path / "integration-output"
    env = os.environ.copy()
    env.update(
        {
            "EVENT_NAME": "pull_request",
            "IS_DRAFT": "false",
            "GITHUB_OUTPUT": str(output),
        }
    )
    script = REPO_ROOT / ".github/scripts/ci/integration-matrix.sh"
    subprocess.run(["bash", str(script)], env=env, check=True, timeout=30)
    matrix_line = next(
        line for line in output.read_text().splitlines() if line.startswith("matrix=")
    )
    matrix = json.loads(matrix_line.removeprefix("matrix="))
    names = {entry["name"] for entry in matrix["include"]}
    assert all(isinstance(name, str) for name in names)

    template = next(
        name
        for job in _jobs(_workflow("integration.yml")).values()
        if isinstance((name := job.get("name")), str) and "matrix.name" in name
    )
    return {template.replace("${{ matrix.name }}", name) for name in names}


def _expected_check_producers(tmp_path: Path) -> dict[str, str]:
    """Derive every required check and its exact producer workflow name."""
    result = _selected_job_producers(DEDICATED_JOBS)
    derived = {
        "ci.yml": _ci_checks(),
        "e2e.yml": _shard_checks("e2e.yml"),
        "e2e-ui.yml": _shard_checks("e2e-ui.yml"),
        "integration.yml": _integration_checks(tmp_path),
    }
    for filename, checks in derived.items():
        producer = _workflow_name(_workflow(filename))
        for check in checks:
            assert check not in result
            result[check] = producer
    return result


def test_required_checks_match_current_workflows(tmp_path: Path) -> None:
    expected_producers = _expected_check_producers(tmp_path)

    required_list = _bash_array("REQUIRED")
    required = set(required_list)
    assert len(required) == len(required_list)
    assert required == set(expected_producers)
    assert _required_workflow_mappings() == expected_producers
    assert "DCO" not in required
    assert "E2E UI Required" not in required


def test_skip_policy_and_completion_triggers_match_producers(tmp_path: Path) -> None:
    required = set(_bash_array("REQUIRED"))
    allow_skip_list = _bash_array("ALLOW_SKIP")
    allow_skip = set(allow_skip_list)
    mappings = _allow_skip_mappings()
    expected_producers = _expected_check_producers(tmp_path)
    assert len(allow_skip) == len(allow_skip_list)
    assert allow_skip <= required
    assert required - allow_skip == set(_selected_job_producers(NON_SKIPPABLE_JOBS))
    assert mappings == {check: expected_producers[check] for check in allow_skip}

    merge_ready = _workflow("merge-ready.yml")
    triggers = merge_ready["on"]["workflow_run"]["workflows"]
    assert isinstance(triggers, list)
    assert len(triggers) == len(set(triggers))
    assert set(triggers) == set(expected_producers.values())
    assert "E2E UI Required" not in triggers


def test_status_lifecycle_writers_share_sha_and_overwrite_same_context(
    tmp_path: Path,
) -> None:
    merge_ready = _workflow("merge-ready.yml")
    workflow_run = merge_ready["on"]["workflow_run"]
    assert workflow_run["types"] == ["requested", "in_progress", "completed"]
    assert merge_ready["on"]["repository_dispatch"]["types"] == ["merge-ready-request"]
    assert "pull_request_target" not in merge_ready["on"]
    assert "concurrency" not in merge_ready

    lifecycle = _workflow_step(merge_ready, "Start Merge Ready status lifecycle")
    checkout = _workflow_step(merge_ready, "Check out scripts")
    context = _workflow_step(merge_ready, "Resolve PR + head SHA")
    final = _workflow_step(merge_ready, "Post Merge Ready status on PR head SHA")
    evaluate = _workflow_step(merge_ready, "Evaluate required checks")
    compute = _workflow_step(merge_ready, "Compute gate outcome")
    fail = _workflow_step(merge_ready, "Fail job when gate is red")
    intent = _workflow_step(merge_ready, "Resolve green automerge intent")
    enable = _workflow_step(merge_ready, "Enable auto-merge for green intent")

    evaluate_job = _jobs(merge_ready)["evaluate"]
    concurrency = evaluate_job["concurrency"]
    assert concurrency["cancel-in-progress"] == "true"
    concurrency_group = concurrency["group"]
    assert "github.event_name" in concurrency_group
    assert "github.event.workflow_run.head_sha" in concurrency_group
    assert "needs.prepare_dispatch.outputs.sha" in concurrency_group
    assert "github.run_id" in concurrency_group
    assert "needs.prepare_dispatch.result == 'success'" in " ".join(
        str(evaluate_job["if"]).split()
    )

    assert _compact_condition(lifecycle) == (
        "github.event_name == 'workflow_run' || github.event_name == 'repository_dispatch'"
    )
    terminal_handler = (
        "github.event_name == 'workflow_dispatch' || steps.lifecycle.outputs.terminal == 'true'"
    )
    assert _compact_condition(checkout) == terminal_handler
    assert _compact_condition(context) == terminal_handler
    assert _compact_condition(final) == (
        "steps.ctx.outputs.skip != 'true' && "
        "steps.lifecycle.outputs.terminal == 'true' && "
        "( github.event_name == 'workflow_run' || "
        "github.event_name == 'repository_dispatch' )"
    )
    terminal_evaluation = (
        "steps.ctx.outputs.skip != 'true' && "
        "( github.event_name == 'workflow_dispatch' || "
        "steps.lifecycle.outputs.terminal == 'true' )"
    )
    assert _compact_condition(evaluate) == terminal_evaluation
    assert _compact_condition(compute) == terminal_evaluation
    assert "steps.lifecycle.outputs.terminal == 'true'" in _compact_condition(fail)
    assert "github.event_name == 'workflow_dispatch'" in _compact_condition(fail)
    assert "steps.gate.outputs.state == 'success'" in _compact_condition(intent)
    assert _compact_condition(enable) == (
        "steps.lifecycle.outputs.terminal == 'true' && "
        "steps.gate.outputs.state == 'success' && "
        "steps.intent.outputs.eligible == 'true'"
    )

    steps = _jobs(merge_ready)["evaluate"]["steps"]
    assert isinstance(steps, list)
    assert steps.index(lifecycle) < steps.index(checkout) < steps.index(context)
    assert steps.index(final) < steps.index(intent) < steps.index(enable)
    lifecycle_script = lifecycle["run"]
    assert isinstance(lifecycle_script, str)
    assert lifecycle_script.index("statuses/$SHA") < lifecycle_script.index("actions/runs/$RUN_ID")
    assert "steps.ctx" not in lifecycle_script
    assert "github.event.action" not in lifecycle_script
    assert "needs.prepare_dispatch.outputs.sha" in lifecycle["env"]["SHA"]

    context_script = context["run"]
    assert isinstance(context_script, str)
    assert 'SHA="${{ github.event.workflow_run.head_sha }}"' in context_script
    assert "search/issues" not in context_script
    assert "WF_PRS" not in context.get("env", {})

    status_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance((script := step.get("run")), str)
        and "statuses/" in script
    ]
    assert [step["name"] for step in status_steps] == [lifecycle["name"], final["name"]]
    other_job_steps = [
        step
        for job_id, job in _jobs(merge_ready).items()
        if job_id != "evaluate"
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]
    assert all("statuses/" not in str(step.get("run", "")) for step in other_job_steps)
    merge_ready_scripts = REPO_ROOT / ".github/scripts/merge-ready"
    assert all(
        "statuses/" not in script.read_text() for script in merge_ready_scripts.glob("*.sh")
    )
    assert not any(
        isinstance(step, dict) and step.get("name") in {"Read PR labels", "Determine eligibility"}
        for step in steps
    )
    assert "post_red" not in (WORKFLOWS / "merge-ready.yml").read_text()
    assert "automerge" not in _compact_condition(lifecycle)
    assert "automerge" not in _compact_condition(final)

    log_file = tmp_path / "gh-calls.jsonl"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")

if len(sys.argv) > 2 and "/actions/runs/" in sys.argv[2]:
    status = os.environ["FAKE_SOURCE_STATUS"]
    if status == "api-error":
        raise SystemExit(1)
    print(status)
"""
    )
    fake_gh.chmod(0o755)

    requested = _run_status_step(
        lifecycle,
        tmp_path=tmp_path,
        log_file=log_file,
        action="requested",
        source_status="queued",
    )
    assert requested == {"terminal": "false"}

    running = _run_status_step(
        lifecycle,
        tmp_path=tmp_path,
        log_file=log_file,
        action="in_progress",
        source_status="in_progress",
    )
    assert running == {"terminal": "false"}

    rerunning = _run_status_step(
        lifecycle,
        tmp_path=tmp_path,
        log_file=log_file,
        action="completed",
        source_status="in_progress",
    )
    assert rerunning == {"terminal": "false"}

    api_failure = _run_status_step(
        lifecycle,
        tmp_path=tmp_path,
        log_file=log_file,
        action="in_progress",
        source_status="api-error",
    )
    assert api_failure == {"terminal": "false"}

    completed = _run_status_step(
        lifecycle,
        tmp_path=tmp_path,
        log_file=log_file,
        action="completed",
    )
    assert completed == {"terminal": "true"}
    _run_status_step(
        final,
        tmp_path=tmp_path,
        log_file=log_file,
        state="success",
        description="All required checks green",
    )

    delayed = _run_status_step(
        lifecycle,
        tmp_path=tmp_path,
        log_file=log_file,
        action="in_progress",
        source_status="completed",
    )
    assert delayed == {"terminal": "true"}
    _run_status_step(
        final,
        tmp_path=tmp_path,
        log_file=log_file,
        state="failure",
        description="Required checks not all green",
    )

    requested_reconcile = _run_status_step(
        lifecycle,
        tmp_path=tmp_path,
        log_file=log_file,
        event_name="repository_dispatch",
    )
    assert requested_reconcile == {"terminal": "true"}
    _run_status_step(
        final,
        tmp_path=tmp_path,
        log_file=log_file,
        state="success",
        description="All required checks green",
    )

    calls = [json.loads(line) for line in log_file.read_text().splitlines()]
    status_calls = [call for call in calls if call[:3] == ["api", "--method", "POST"]]
    source_calls = [
        call
        for call in calls
        if len(call) > 1 and call[0] == "api" and "/actions/runs/" in call[1]
    ]
    assert ["status" if call in status_calls else "source" for call in calls] == (
        ["status", "source"] * 4 + ["status", "source", "status"] * 2 + ["status", "status"]
    )
    assert (
        source_calls
        == [["api", "repos/example/omnigent/actions/runs/123456", "--jq", ".status"]] * 6
    )

    parsed = [_status_call(call) for call in status_calls]
    expected_endpoint = "repos/example/omnigent/statuses/0123456789abcdef0123456789abcdef01234567"
    assert [endpoint for endpoint, _fields in parsed] == [expected_endpoint] * 10
    assert [fields["context"] for _endpoint, fields in parsed] == ["Merge Ready"] * 10
    assert [fields["state"] for _endpoint, fields in parsed] == [
        "pending",
        "pending",
        "pending",
        "pending",
        "pending",
        "success",
        "pending",
        "failure",
        "pending",
        "success",
    ]
    assert all(len(fields["description"]) <= 140 for _endpoint, fields in parsed)

    effective: dict[tuple[str, str], str] = {}
    for endpoint, fields in parsed[:6]:
        effective[(endpoint, fields["context"])] = fields["state"]
    assert effective[(expected_endpoint, "Merge Ready")] == "success"
    for endpoint, fields in parsed[6:]:
        effective[(endpoint, fields["context"])] = fields["state"]
    assert effective[(expected_endpoint, "Merge Ready")] == "success"
    assert parsed[7][1]["state"] == "failure"
    assert parsed[8][1]["state"] == "pending"


def test_command_and_status_jobs_have_least_privilege_and_separate_lifecycles() -> None:
    merge_ready = _workflow("merge-ready.yml")
    jobs = _jobs(merge_ready)
    assert set(jobs) == {"command", "prepare_dispatch", "evaluate"}
    assert jobs["command"]["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "read",
    }
    assert jobs["prepare_dispatch"]["permissions"] == {}
    assert jobs["evaluate"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "read",
        "checks": "read",
        "actions": "read",
        "statuses": "write",
    }

    command_if = " ".join(str(jobs["command"]["if"]).split())
    assert "github.event_name == 'issue_comment'" in command_if
    assert "author_association == 'OWNER'" in command_if
    prepare_if = " ".join(str(jobs["prepare_dispatch"]["if"]).split())
    assert prepare_if == (
        "github.event_name == 'repository_dispatch' && "
        "github.event.action == 'merge-ready-request'"
    )
    evaluate_if = " ".join(str(jobs["evaluate"]["if"]).split())
    assert "github.event.workflow_run.event == 'pull_request'" in evaluate_if
    assert "needs.prepare_dispatch.result == 'success'" in evaluate_if

    command_steps = jobs["command"]["steps"]
    assert isinstance(command_steps, list)
    assert not any("statuses/" in str(step.get("run", "")) for step in command_steps)
    assert not any("gh pr merge" in str(step.get("run", "")) for step in command_steps)
    record = _workflow_step(
        merge_ready,
        "Record /merge intent and request reconciliation",
        job_id="command",
    )
    assert record["run"] == "bash .github/scripts/merge-ready/enable-automerge-comment.sh"

    comment_script = COMMENT_INTENT_SH.read_text()
    assert "repos/$REPO/issues/$PR/labels" in comment_script
    assert "repos/$REPO/dispatches" in comment_script
    assert "event_type=merge-ready-request" in comment_script
    assert "gh pr merge" not in comment_script
    label_script = LABEL_INTENT_SH.read_text()
    assert '--match-head-commit "$SHA"' in label_script
    assert "--delete-branch" not in label_script
    assert "--admin" not in label_script
    assert "clean status" not in label_script


def test_merge_comment_records_intent_and_dispatches_without_merging(tmp_path: Path) -> None:
    log_file = tmp_path / "gh-calls.jsonl"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if "repos/example/omnigent/dispatches" in args and os.environ.get("FAIL_DISPATCH") == "1":
    raise SystemExit(1)
"""
    )
    fake_gh.chmod(0o755)
    sha = "0123456789abcdef0123456789abcdef01234567"
    env = os.environ.copy()
    env.update(
        {
            "AUTHOR": "maintainer",
            "FAKE_GH_LOG": str(log_file),
            "GH_TOKEN": "test-token",
            "PATH": f"{tmp_path}:{env['PATH']}",
            "PR": "42",
            "REPO": "example/omnigent",
            "SHA": sha,
        }
    )

    subprocess.run(
        ["bash", str(COMMENT_INTENT_SH)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert calls[:2] == [
        [
            "api",
            "--method",
            "POST",
            "repos/example/omnigent/issues/42/labels",
            "-f",
            "labels[]=automerge",
        ],
        [
            "api",
            "--method",
            "POST",
            "repos/example/omnigent/dispatches",
            "-f",
            "event_type=merge-ready-request",
            "-f",
            "client_payload[pr]=42",
            "-f",
            f"client_payload[sha]={sha}",
        ],
    ]
    assert calls[2][:5] == ["pr", "comment", "42", "--repo", "example/omnigent"]
    assert "re-evaluating that exact head" in calls[2][-1]
    assert not any(call[:2] == ["pr", "merge"] for call in calls)
    assert not any("statuses/" in argument for call in calls for argument in call)

    log_file.write_text("")
    failed_env = env | {"FAIL_DISPATCH": "1"}
    failed = subprocess.run(
        ["bash", str(COMMENT_INTENT_SH)],
        env=failed_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert failed.returncode != 0
    failed_calls = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(failed_calls) == 2
    assert not any(call[:2] == ["pr", "comment"] for call in failed_calls)
    assert not any(call[:2] == ["pr", "merge"] for call in failed_calls)


def test_dispatch_payload_validation_only_blocks_invalid_sha(tmp_path: Path) -> None:
    merge_ready = _workflow("merge-ready.yml")
    payload = _workflow_step(
        merge_ready,
        "Validate reconciliation payload",
        job_id="prepare_dispatch",
    )
    sha = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    valid_dir = tmp_path / "valid"
    valid_dir.mkdir()
    valid, outputs = _run_inline_step(
        payload,
        tmp_path=valid_dir,
        env_update={"REQUEST_PR": "42", "REQUEST_SHA": sha},
    )
    assert valid.returncode == 0
    assert outputs == {"pr": "42", "sha": sha.lower()}

    missing_pr_dir = tmp_path / "missing-pr"
    missing_pr_dir.mkdir()
    missing_pr, outputs = _run_inline_step(
        payload,
        tmp_path=missing_pr_dir,
        env_update={"REQUEST_PR": "not-a-pr", "REQUEST_SHA": sha},
    )
    assert missing_pr.returncode == 0
    assert outputs == {"pr": "", "sha": sha.lower()}

    invalid_dir = tmp_path / "invalid"
    invalid_dir.mkdir()
    invalid, outputs = _run_inline_step(
        payload,
        tmp_path=invalid_dir,
        env_update={"REQUEST_PR": "42", "REQUEST_SHA": "deadbeef"},
    )
    assert invalid.returncode != 0
    assert outputs == {}


def test_green_intent_resolution_is_cross_fork_safe_and_fail_closed(tmp_path: Path) -> None:
    merge_ready = _workflow("merge-ready.yml")
    intent = _workflow_step(merge_ready, "Resolve green automerge intent")
    sha = "0123456789abcdef0123456789abcdef01234567"

    def run_case(
        name: str,
        *,
        search: str,
        prs: dict[str, Any],
        event_name: str = "workflow_run",
        request_pr: str = "",
        search_error: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[list[str]]]:
        case_dir = tmp_path / name
        case_dir.mkdir()
        log_file = case_dir / "gh-calls.jsonl"
        fake_gh = case_dir / "gh"
        fake_gh.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if args and args[0] == "api" and "search/issues" in args:
    if os.environ.get("FAKE_SEARCH_ERROR") == "1":
        raise SystemExit(1)
    print(os.environ.get("FAKE_SEARCH", ""))
elif args[:2] == ["pr", "view"]:
    value = json.loads(os.environ["FAKE_PRS"]).get(args[2])
    if value is None:
        raise SystemExit(1)
    print(json.dumps(value))
else:
    raise SystemExit(2)
"""
        )
        fake_gh.chmod(0o755)
        proc, outputs = _run_inline_step(
            intent,
            tmp_path=case_dir,
            env_update={
                "EVENT_NAME": event_name,
                "FAKE_GH_LOG": str(log_file),
                "FAKE_PRS": json.dumps(prs),
                "FAKE_SEARCH": search,
                "FAKE_SEARCH_ERROR": "1" if search_error else "0",
                "REPO": "example/omnigent",
                "REQUEST_PR": request_pr,
                "SHA": sha,
            },
        )
        calls = [json.loads(line) for line in log_file.read_text().splitlines()]
        return proc, outputs, calls

    labeled = {"state": "OPEN", "headRefOid": sha, "labels": [{"name": "automerge"}]}
    success, outputs, calls = run_case("success", search="42", prs={"42": labeled})
    assert success.returncode == 0
    assert outputs == {"eligible": "true", "pr": "42"}
    assert calls[0] == [
        "api",
        "--method",
        "GET",
        "--paginate",
        "search/issues",
        "-f",
        f"q=repo:example/omnigent type:pr state:open sha:{sha}",
        "-f",
        "per_page=100",
        "--jq",
        ".items[].number",
    ]
    assert "commits/" not in " ".join(calls[0])

    stale = labeled | {"headRefOid": "f" * 40}
    _, outputs, _ = run_case("stale", search="42", prs={"42": stale})
    assert outputs == {"eligible": "false"}
    _, outputs, _ = run_case("multiple", search="42\n43", prs={"42": labeled, "43": labeled})
    assert outputs == {"eligible": "false"}
    _, outputs, _ = run_case("lookup-error", search="42", prs={})
    assert outputs == {"eligible": "false"}
    _, outputs, _ = run_case("search-error", search="", prs={}, search_error=True)
    assert outputs == {"eligible": "false"}
    unlabeled = labeled | {"labels": []}
    _, outputs, _ = run_case("unlabeled", search="42", prs={"42": unlabeled})
    assert outputs == {"eligible": "false"}
    _, outputs, _ = run_case(
        "request-mismatch",
        search="42",
        prs={"42": labeled},
        event_name="repository_dispatch",
        request_pr="99",
    )
    assert outputs == {"eligible": "false"}


def test_automerge_helper_revalidates_and_never_falls_back(tmp_path: Path) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"

    def run_case(
        name: str,
        pr_data: dict[str, Any],
        *,
        merge_failure: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        case_dir = tmp_path / name
        case_dir.mkdir()
        log_file = case_dir / "gh-calls.jsonl"
        fake_gh = case_dir / "gh"
        fake_gh.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:2] == ["pr", "view"]:
    print(os.environ["FAKE_PR_JSON"])
elif args[:2] == ["pr", "merge"]:
    if os.environ.get("FAKE_MERGE_FAILURE") == "1":
        print("head changed", file=sys.stderr)
        raise SystemExit(1)
    print("queued")
else:
    raise SystemExit(2)
"""
        )
        fake_gh.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "FAKE_GH_LOG": str(log_file),
                "FAKE_MERGE_FAILURE": "1" if merge_failure else "0",
                "FAKE_PR_JSON": json.dumps(pr_data),
                "GH_TOKEN": "test-token",
                "PATH": f"{case_dir}:{env['PATH']}",
                "PR": "42",
                "REPO": "example/omnigent",
                "SHA": sha,
            }
        )
        proc = subprocess.run(
            ["bash", str(LABEL_INTENT_SH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        calls = [json.loads(line) for line in log_file.read_text().splitlines()]
        return proc, calls

    valid = {"state": "OPEN", "headRefOid": sha, "labels": [{"name": "automerge"}]}
    success, calls = run_case("success", valid)
    assert success.returncode == 0
    assert calls[1] == [
        "pr",
        "merge",
        "42",
        "--repo",
        "example/omnigent",
        "--squash",
        "--auto",
        "--match-head-commit",
        sha,
    ]
    assert "--delete-branch" not in calls[1]
    assert "--admin" not in calls[1]

    failed, calls = run_case("head-race", valid, merge_failure=True)
    assert failed.returncode == 0
    assert len([call for call in calls if call[:2] == ["pr", "merge"]]) == 1
    advanced = valid | {"headRefOid": "f" * 40}
    _, calls = run_case("advanced", advanced)
    assert [call[:2] for call in calls] == [["pr", "view"]]
    unlabeled = valid | {"labels": []}
    _, calls = run_case("unlabeled", unlabeled)
    assert [call[:2] for call in calls] == [["pr", "view"]]


def test_allow_skip_does_not_mask_emitted_ui_snapshot_failure(
    tmp_path: Path,
) -> None:
    """The docs-routing aggregate remains red even while absence is skippable."""
    mappings = _required_workflow_mappings()
    producers = sorted(set(mappings.values()))
    suites = {producer: str(1000 + index) for index, producer in enumerate(producers)}
    checks = []
    for index, check in enumerate(_bash_array("REQUIRED")):
        suite = suites[mappings[check]]
        if check == "UI Snapshot (visual baselines)":
            # Equal-second rows must select the higher check-run ID, not GNU
            # sort's whole-line fallback (which would prefer "success").
            checks.append(f"{check}\tcompleted\tsuccess\t2026-08-20T00:30:00Z\t{suite}\t4000")
            checks.append(f"{check}\tcompleted\tfailure\t2026-08-20T00:30:00Z\t{suite}\t4001")
        else:
            checks.append(
                f"{check}\tcompleted\tsuccess\t2026-08-20T00:30:00Z\t{suite}\t{4100 + index}"
            )
    workflow_runs = [
        f"{producer}\tcompleted\tsuccess\t2026-08-20T00:{index:02d}:00Z"
        f"\t{suites[producer]}\t{5000 + index}"
        for index, producer in enumerate(producers)
    ]

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
    )

    assert proc.returncode == 1
    assert "NOT GREEN: UI Snapshot (visual baselines)" in proc.stdout
    assert "`UI Snapshot (visual baselines)` (conclusion=failure)" in output


def test_old_success_cannot_vouch_for_newer_in_progress_producer(
    tmp_path: Path,
) -> None:
    mappings = _required_workflow_mappings()
    producers = sorted(set(mappings.values()))
    suites = {producer: str(2000 + index) for index, producer in enumerate(producers)}
    old_ci_suite = "1999"
    checks = [
        (
            f"{check}\tcompleted\tsuccess\t2026-08-20T00:10:00Z\t"
            f"{old_ci_suite if mappings[check] == 'CI' else suites[mappings[check]]}"
            f"\t{6000 + index}"
        )
        for index, check in enumerate(_bash_array("REQUIRED"))
    ]
    workflow_runs = [
        (
            f"{producer}\t{'in_progress' if producer == 'CI' else 'completed'}\t"
            f"{'null' if producer == 'CI' else 'success'}\t"
            f"2026-08-20T00:{index:02d}:00Z\t{suites[producer]}\t{7000 + index}"
        )
        for index, producer in enumerate(producers)
    ]

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
    )

    assert proc.returncode == 1
    assert "pull_request workflow 'CI' is status=in_progress" in proc.stdout
    assert "producer workflow is status=in_progress, conclusion=null" in output
    script = EVALUATE_SH.read_text()
    assert 'select(.event == "pull_request")' in script
    assert ".check_suite.id" in script
    assert ".check_suite_id" in script
    assert "filter=all&per_page=100" in script
    assert "-k6,6n" in script
    assert ".run_attempt" in script
    assert "/attempts/$run_attempt/jobs?per_page=100" in script
    assert ".check_run_url" in script


def test_equal_timestamp_run_id_selects_newest_producer_suite(
    tmp_path: Path,
) -> None:
    mappings = _required_workflow_mappings()
    producers = sorted(set(mappings.values()))
    suites = {producer: str(10000 + index) for index, producer in enumerate(producers)}
    old_ci_suite = "99999"
    target = "Pytest (runtime-harnesses)"
    checks: list[str] = []
    for index, check in enumerate(_bash_array("REQUIRED")):
        producer = mappings[check]
        if producer == "CI":
            checks.append(
                f"{check}\tcompleted\tsuccess\t2026-08-20T00:20:00Z"
                f"\t{old_ci_suite}\t{10000 + index}"
            )
        conclusion = "failure" if check == target else "success"
        checks.append(
            f"{check}\tcompleted\t{conclusion}\t2026-08-20T00:30:00Z"
            f"\t{suites[producer]}\t{11000 + index}"
        )

    workflow_runs = [
        (
            f"{producer}\tcompleted\t{'failure' if producer == 'CI' else 'success'}"
            f"\t2026-08-20T00:{index:02d}:00Z\t{suites[producer]}\t{12000 + index}"
        )
        for index, producer in enumerate(producers)
    ]
    ci_index = producers.index("CI")
    workflow_runs.append(
        f"CI\tcompleted\tsuccess\t2026-08-20T00:{ci_index:02d}:00Z\t{old_ci_suite}\t1"
    )

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
    )

    assert proc.returncode == 1
    assert f"NOT GREEN: {target}" in proc.stdout
    assert f"`{target}` (conclusion=failure)" in output


def test_newer_empty_label_suite_preserves_older_required_failure(
    tmp_path: Path,
) -> None:
    mappings = _required_workflow_mappings()
    producers = sorted(set(mappings.values()))
    suites = {producer: str(13000 + index) for index, producer in enumerate(producers)}
    old_e2e_suite = "12999"
    target = "E2E Tests (shard 0/4)"
    checks: list[str] = []
    for index, check in enumerate(_bash_array("REQUIRED")):
        producer = mappings[check]
        if producer == "E2E Tests":
            conclusion = "failure" if check == target else "success"
            checks.append(
                f"{check}\tcompleted\t{conclusion}\t2026-08-20T00:20:00Z"
                f"\t{old_e2e_suite}\t{13000 + index}"
            )
        else:
            checks.append(
                f"{check}\tcompleted\tsuccess\t2026-08-20T00:30:00Z"
                f"\t{suites[producer]}\t{14000 + index}"
            )

    workflow_runs = [
        (
            f"{producer}\tcompleted\t{'skipped' if producer == 'E2E Tests' else 'success'}"
            f"\t2026-08-20T00:{index:02d}:30Z\t{suites[producer]}\t{15000 + index}"
        )
        for index, producer in enumerate(producers)
    ]
    workflow_runs.append(
        f"E2E Tests\tcompleted\tfailure\t2026-08-20T00:00:00Z\t{old_e2e_suite}\t2"
    )

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
    )

    assert proc.returncode == 1
    assert f"NOT GREEN: {target}" in proc.stdout
    assert f"`{target}` (conclusion=failure)" in output


def test_empty_suite_fallback_prefers_newer_producer_run_over_late_check(
    tmp_path: Path,
) -> None:
    mappings = _required_workflow_mappings()
    producers = sorted(set(mappings.values()))
    suites = {producer: str(18000 + index) for index, producer in enumerate(producers)}
    older_e2e_suite = "17998"
    newer_e2e_suite = "17999"
    target = "E2E Tests (shard 0/4)"
    checks: list[str] = []
    for index, check in enumerate(_bash_array("REQUIRED")):
        producer = mappings[check]
        if producer == "E2E Tests":
            conclusion = "failure" if check == target else "success"
            checks.append(
                f"{check}\tcompleted\t{conclusion}\t2026-08-20T00:30:00Z"
                f"\t{newer_e2e_suite}\t{18000 + index}"
            )
            # The older run finishes later. Check completion time must not let
            # it override the newer producer run's decisive failure.
            checks.append(
                f"{check}\tcompleted\tsuccess\t2026-08-20T01:00:00Z"
                f"\t{older_e2e_suite}\t{19000 + index}"
            )
        else:
            checks.append(
                f"{check}\tcompleted\tsuccess\t2026-08-20T00:30:00Z"
                f"\t{suites[producer]}\t{20000 + index}"
            )

    workflow_runs = [
        (
            f"{producer}\tcompleted\t{'skipped' if producer == 'E2E Tests' else 'success'}"
            f"\t2026-08-20T00:20:{index:02d}Z\t{suites[producer]}\t{21000 + index}"
        )
        for index, producer in enumerate(producers)
    ]
    workflow_runs.extend(
        [
            f"E2E Tests\tcompleted\tsuccess\t2026-08-20T00:00:00Z\t{older_e2e_suite}\t10",
            f"E2E Tests\tcompleted\tfailure\t2026-08-20T00:10:00Z\t{newer_e2e_suite}\t11",
        ]
    )

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
    )

    assert proc.returncode == 1
    assert f"NOT GREEN: {target}" in proc.stdout
    assert f"`{target}` (conclusion=failure)" in output


def test_empty_suite_fallback_stops_at_newer_failed_producer_without_row(
    tmp_path: Path,
) -> None:
    mappings = _required_workflow_mappings()
    producers = sorted(set(mappings.values()))
    suites = {producer: str(22000 + index) for index, producer in enumerate(producers)}
    older_e2e_suite = "21998"
    failed_e2e_suite = "21999"
    target = "E2E Tests (shard 0/4)"
    checks = [
        (
            f"{check}\tcompleted\tsuccess\t2026-08-20T01:00:00Z\t"
            f"{older_e2e_suite if mappings[check] == 'E2E Tests' else suites[mappings[check]]}"
            f"\t{22000 + index}"
        )
        for index, check in enumerate(_bash_array("REQUIRED"))
    ]
    workflow_runs = [
        (
            f"{producer}\tcompleted\t{'skipped' if producer == 'E2E Tests' else 'success'}"
            f"\t2026-08-20T00:20:{index:02d}Z\t{suites[producer]}\t{23000 + index}"
        )
        for index, producer in enumerate(producers)
    ]
    workflow_runs.extend(
        [
            f"E2E Tests\tcompleted\tsuccess\t2026-08-20T00:00:00Z\t{older_e2e_suite}\t20",
            f"E2E Tests\tcompleted\tfailure\t2026-08-20T00:10:00Z\t{failed_e2e_suite}\t21",
        ]
    )

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
    )

    assert proc.returncode == 1
    assert f"NOT GREEN: {target}" in proc.stdout
    assert "conclusion=failure with no check row" in proc.stdout
    assert "conclusion=failure and emitted no matching check" in output


def test_any_active_producer_run_blocks_newer_empty_label_suite(
    tmp_path: Path,
) -> None:
    mappings = _required_workflow_mappings()
    producers = sorted(set(mappings.values()))
    suites = {producer: str(16000 + index) for index, producer in enumerate(producers)}
    active_e2e_suite = "15999"
    checks = [
        (
            f"{check}\tcompleted\tsuccess\t2026-08-20T00:20:00Z\t"
            f"{active_e2e_suite if mappings[check] == 'E2E Tests' else suites[mappings[check]]}"
            f"\t{16000 + index}"
        )
        for index, check in enumerate(_bash_array("REQUIRED"))
    ]
    workflow_runs = [
        (
            f"{producer}\tcompleted\t{'skipped' if producer == 'E2E Tests' else 'success'}"
            f"\t2026-08-20T00:{index:02d}:30Z\t{suites[producer]}\t{17000 + index}"
        )
        for index, producer in enumerate(producers)
    ]
    workflow_runs.append(
        f"E2E Tests\tin_progress\tnull\t2026-08-20T00:00:00Z\t{active_e2e_suite}\t3"
    )

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
    )

    assert proc.returncode == 1
    assert "pull_request workflow 'E2E Tests' is status=in_progress" in proc.stdout
    assert "producer workflow is status=in_progress, conclusion=null" in output


def test_current_suite_success_and_conditional_absence_are_green(
    tmp_path: Path,
) -> None:
    mappings = _required_workflow_mappings()
    producers = sorted(set(mappings.values()))
    suites = {producer: str(3000 + index) for index, producer in enumerate(producers)}
    non_skippable = set(_bash_array("REQUIRED")) - set(_bash_array("ALLOW_SKIP"))
    checks = [
        (
            f"{check}\tcompleted\tsuccess\t2026-08-20T00:30:00Z"
            f"\t{suites[mappings[check]]}\t{8000 + index}"
        )
        for index, check in enumerate(
            sorted(
                check
                for check in _bash_array("REQUIRED")
                if check in non_skippable or mappings[check] == "CI"
            )
        )
    ]
    # A manual suite with the same check name must be ignored by fallback.
    checks.append("Docker build\tcompleted\tfailure\t2026-08-20T00:31:00Z\t99999\t8999")
    workflow_runs = [
        (
            f"{producer}\tcompleted\t"
            f"{'failure' if producer == 'CI' else 'success'}\t"
            f"2026-08-20T00:{index:02d}:00Z\t{suites[producer]}\t{9000 + index}"
        )
        for index, producer in enumerate(producers)
    ]

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
    )

    assert proc.returncode == 0, proc.stdout
    assert "All required checks green" in proc.stdout
    assert output == "failed<<_FAILED_EOF_\n_FAILED_EOF_\n"


def test_rerun_rejects_prior_attempt_success_in_reused_suite(tmp_path: Path) -> None:
    checks, workflow_runs, check_ids, run_ids, suites, mappings = _successful_evaluator_fixture(
        30000
    )
    target = "Pytest (runtime-harnesses)"
    ci_run_id = run_ids["CI"]
    for index, row in enumerate(workflow_runs):
        fields = row.split("\t")
        if fields[0] == "CI":
            fields[2] = "failure"
            fields[6] = "2"
            workflow_runs[index] = "\t".join(fields)
            break
    current_attempt_ids = [
        check_ids[check]
        for check in _bash_array("REQUIRED")
        if mappings[check] == "CI" and check != target
    ]

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
        attempt_jobs={(ci_run_id, "2"): current_attempt_ids},
    )

    assert proc.returncode == 1
    assert f"NOT GREEN: {target}" in proc.stdout
    assert "current-suite check is missing" in output
    assert check_ids[target] not in current_attempt_ids
    assert suites["CI"] in next(row for row in checks if row.startswith(f"{target}\t"))


def test_exact_attempt_jobs_api_failure_is_fail_closed(tmp_path: Path) -> None:
    checks, workflow_runs, _, run_ids, _, _ = _successful_evaluator_fixture(40000)

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
        attempt_jobs={(run_ids["Lint"], "1"): None},
    )

    assert proc.returncode == 1
    assert "Could not load exact-attempt jobs" in proc.stderr
    assert "could not inspect the producer's exact workflow attempt" in proc.stdout
    assert "Pre-commit checks" in output


def test_malformed_exact_attempt_check_id_is_fail_closed(tmp_path: Path) -> None:
    checks, workflow_runs, _, run_ids, _, _ = _successful_evaluator_fixture(50000)

    proc, output = _run_evaluate(
        tmp_path,
        checks=checks,
        workflow_runs=workflow_runs,
        attempt_jobs={(run_ids["Lint"], "1"): ["not-an-id"]},
    )

    assert proc.returncode == 1
    assert "Malformed check-run ID" in proc.stderr
    assert "could not inspect the producer's exact workflow attempt" in proc.stdout
    assert "Pre-commit checks" in output
