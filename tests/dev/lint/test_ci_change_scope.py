"""Regression tests for documentation-only CI routing."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_POLICY_PATH = _REPO_ROOT / ".github" / "scripts" / "ci" / "change_scope.py"


def _load_policy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_change_scope", _POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POLICY = _load_policy()


def _workflow(name: str) -> dict[object, object]:
    path = _REPO_ROOT / ".github" / "workflows" / name
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers(name: str) -> dict[object, object]:
    loaded = _workflow(name)
    # YAML 1.1 treats the unquoted workflow key `on` as boolean true.
    triggers = loaded.get("on", loaded.get(True))
    assert isinstance(triggers, dict)
    return triggers


def test_docs_only_classification_is_nonempty_and_markdown_only() -> None:
    assert POLICY.is_docs_only(["README.md", "docs/runbook.md", "AGENTS.md"])
    assert not POLICY.is_docs_only(["web/README.MD"])
    assert not POLICY.is_docs_only([])
    assert not POLICY.is_docs_only(["README.md", "omnigent/runtime/workflow.py"])


def test_heavy_workflow_events_share_the_docs_only_policy() -> None:
    for workflow_name, expected_events in POLICY.EVENT_FILTERED_HEAVY_WORKFLOWS.items():
        triggers = _triggers(workflow_name)
        for event, expected in expected_events.items():
            config = triggers[event]
            assert isinstance(config, dict)
            ignored = config.get("paths-ignore", [])
            assert isinstance(ignored, list)
            assert all(isinstance(path, str) for path in ignored)
            assert ignored == list(expected), f"{workflow_name}:{event} drifted"


def test_lint_and_security_still_run_for_docs_only_changes() -> None:
    for workflow_name in POLICY.ALWAYS_RUN_FOR_DOCS:
        pull_request = _triggers(workflow_name)["pull_request"]
        if isinstance(pull_request, dict):
            assert "paths" not in pull_request
            assert "paths-ignore" not in pull_request


def test_named_required_checks_remain_job_filtered() -> None:
    for workflow_name in POLICY.JOB_FILTERED_HEAVY_WORKFLOWS:
        pull_request = _triggers(workflow_name)["pull_request"]
        assert isinstance(pull_request, dict)
        assert "paths" not in pull_request
        assert "paths-ignore" not in pull_request

        jobs = _workflow(workflow_name)["jobs"]
        assert isinstance(jobs, dict)
        render = jobs["render"]
        gate = jobs["ui-snapshot"]
        assert isinstance(render, dict)
        assert isinstance(gate, dict)
        assert render["if"] == "${{ needs.detect.outputs.ui == 'true' }}"
        assert gate["name"] == "UI Snapshot (visual baselines)"
        assert gate["if"] == "${{ always() }}"
        assert set(gate["needs"]) == {"detect", "render"}


def test_ui_snapshot_required_gate_fails_closed() -> None:
    jobs = _workflow("ui-snapshot.yml")["jobs"]
    assert isinstance(jobs, dict)
    gate = jobs["ui-snapshot"]
    assert isinstance(gate, dict)
    steps = gate["steps"]
    assert isinstance(steps, list) and len(steps) == 1
    script = steps[0]["run"]
    assert isinstance(script, str)

    cases = (
        ("success", "false", "skipped", 0),
        ("success", "true", "success", 0),
        ("failure", "", "skipped", 1),
        ("success", "", "skipped", 1),
        ("success", "invalid", "skipped", 1),
        ("success", "true", "failure", 1),
        ("success", "true", "cancelled", 1),
    )
    for detect_result, ui_required, render_result, expected in cases:
        result = subprocess.run(
            ["bash", "-eu", "-o", "pipefail", "-c", script],
            cwd=_REPO_ROOT,
            env={
                **os.environ,
                "DETECT_RESULT": detect_result,
                "UI_REQUIRED": ui_required,
                "RENDER_RESULT": render_result,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stdout + result.stderr


def test_merge_ready_accepts_absent_path_filtered_workflows(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        r"""#!/usr/bin/env bash
set -euo pipefail

: "${REQUIRED_SH:?}"
# shellcheck disable=SC1090
source "$REQUIRED_SH"

declare -A producer_suites=()
declare -A producer_runs=()
declare -A check_ids=()
producers=()
next_suite=1000
next_check=2000
next_run=3000
for check in "${REQUIRED[@]}"; do
  if is_allow_skip "$check"; then
    continue
  fi
  next_check=$((next_check + 1))
  check_ids["$check"]=$next_check
  producer=$(workflow_for "$check")
  if [[ -n "$producer" && -z "${producer_suites[$producer]+x}" ]]; then
    next_suite=$((next_suite + 1))
    next_run=$((next_run + 1))
    producer_suites["$producer"]=$next_suite
    producer_runs["$producer"]=$next_run
    producers+=("$producer")
  fi
done

endpoint=${2:-}
jobs_prefix="repos/$REPO/actions/runs/"
if [[ "$endpoint" == "$jobs_prefix"*"/jobs?per_page=100" ]]; then
  jobs_path=${endpoint#"$jobs_prefix"}
  run_id=${jobs_path%%/*}
  jobs_path=${jobs_path#"$run_id/attempts/"}
  run_attempt=${jobs_path%%/*}
  if [[ "$endpoint" != "$jobs_prefix$run_id/attempts/$run_attempt/jobs?per_page=100" ]]; then
    exit 2
  fi
  if ! [[ "$run_id" =~ ^[1-9][0-9]*$ && "$run_attempt" == "1" ]]; then
    exit 2
  fi
  for producer in "${producers[@]}"; do
    if [[ "${producer_runs[$producer]}" != "$run_id" ]]; then
      continue
    fi
    for check in "${REQUIRED[@]}"; do
      if ! is_allow_skip "$check" && [[ "$(workflow_for "$check")" == "$producer" ]]; then
        printf '%s\n' "${check_ids[$check]}"
      fi
    done
    exit 0
  done
  exit 2
fi

case "${2:-}" in
  "repos/$REPO/commits/$SHA/check-runs?filter=all&per_page=100" | \
  "repos/$REPO/commits/$SHA/check-runs")
    for check in "${REQUIRED[@]}"; do
      if is_allow_skip "$check"; then
        continue
      fi
      producer=$(workflow_for "$check")
      suite=9000
      if [[ -n "$producer" ]]; then
        suite=${producer_suites[$producer]}
      fi
      printf '%s\tcompleted\tsuccess\t2026-08-20T00:00:00Z\t%s\t%s\n' \
        "$check" "$suite" "${check_ids[$check]}"
    done
    ;;
  "repos/$REPO/actions/runs?head_sha=$SHA&per_page=100")
    for producer in "${producers[@]}"; do
      printf '%s\tcompleted\tsuccess\t2026-08-20T00:00:00Z\t%s\t%s\t1\n' \
        "$producer" "${producer_suites[$producer]}" "${producer_runs[$producer]}"
    done
    ;;
  *)
    printf 'unexpected gh invocation: %s\n' "$*" >&2
    exit 2
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    output = tmp_path / "github-output"
    script = _REPO_ROOT / ".github" / "scripts" / "merge-ready" / "evaluate-checks.sh"
    result = subprocess.run(
        [str(script)],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "REQUIRED_SH": str(_REPO_ROOT / ".github" / "scripts" / "merge-ready" / "required.sh"),
            "GH_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(output),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "REPO": "example/repo",
            "SHA": "deadbeef",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All required checks green" in result.stdout
    assert "failed<<_FAILED_EOF_" in output.read_text(encoding="utf-8")


def test_ui_snapshot_scope_skips_docs_but_fails_open() -> None:
    assert not POLICY.affects_ui_snapshot(["web/README.md"])
    assert not POLICY.affects_ui_snapshot(["docs/runbook.md", "AGENTS.md"])
    assert POLICY.affects_ui_snapshot(["web/src/App.tsx"])
    assert POLICY.affects_ui_snapshot(["README.md", "web/src/App.tsx"])
    assert POLICY.affects_ui_snapshot([".github/workflows/ui-snapshot.yml"])
    assert POLICY.affects_ui_snapshot([])


def test_ui_snapshot_pr_files_includes_renamed_source_path() -> None:
    moved_out = ['{"filename":"archive/App.tsx","previous_filename":"web/src/App.tsx"}']
    assert POLICY.affects_ui_snapshot_pr_files(moved_out, expected_count=1)


def test_ui_snapshot_pr_files_fails_open_on_truncation_or_malformed_data() -> None:
    docs = ['{"filename":"web/README.md","previous_filename":null}']
    assert not POLICY.affects_ui_snapshot_pr_files(docs, expected_count=1)
    assert POLICY.affects_ui_snapshot_pr_files(docs, expected_count=2)
    assert POLICY.affects_ui_snapshot_pr_files(["not-json"], expected_count=1)
