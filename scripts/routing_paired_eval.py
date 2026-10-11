"""Replay source-only repository tasks with native Codex and independent graders.

The caller supplies a private JSON manifest with pinned repositories, profiles,
prompts and verifier paths. No model IDs, credentials or historical prompts live
in this script. Fixtures are validated against both the broken starting tree and
the accepted reference before inference. Only allowlisted source files cross
from the model's workspace into a fresh grading tree.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def run(argv: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, **kwargs)


def bounded_process(command: list[str], *, timeout: float, input_bytes=None, **kwargs):
    """Stop the owned process group before reading a timed-out submission."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, start_new_session=True, **kwargs)
    try:
        process.communicate(input=input_bytes, timeout=timeout)
        return process.returncode, False
    except subprocess.TimeoutExpired:
        # Killing just the CLI can leave its tool subprocess writing source
        # while the independent grader copies it. This group belongs to this run.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The group leader can exit before a tool child. Reap the whole
            # owned group even when communicate already returned.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        return process.returncode, True


def archive(repo: Path, ref: str) -> tuple[str, bytes]:
    sha = run(["git", "rev-parse", ref + "^{commit}"], repo).stdout.decode().strip()
    return sha, run(["git", "archive", "--format=tar", sha], repo).stdout


def extract(data: bytes, target: Path) -> None:
    target.mkdir(mode=0o700, parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
        bundle.extractall(target, filter="data")


def relative_file(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Fixture file must be a relative path without parent traversal")
    return path


def copy_source(source: Path, target: Path, paths: list[str]) -> None:
    for value in paths:
        path = relative_file(value)
        src = source / path
        # Do not import symlinks to another arm, the reference, or user state.
        if any(part.is_symlink() for part in [src, *src.parents] if part != source.parent):
            raise ValueError(f"Symlink in submitted source: {value}")
        dest = target / path
        if src.exists():
            if not src.is_file():
                raise ValueError(f"Submitted source is not a file: {value}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        else:
            dest.unlink(missing_ok=True)


def install_verifiers(repo: Path, ref: str, target: Path, paths: list[str]) -> dict[str, str]:
    hashes = {}
    for value in paths:
        path = relative_file(value)
        content = run(["git", "show", f"{ref}:{path.as_posix()}"], repo).stdout
        dest = target / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        hashes[value] = hashlib.sha256(content).hexdigest()
    return hashes


def junit_result(path: Path, minimum: int) -> dict:
    if not path.exists():
        return {"passed": False, "tests": 0, "reason": "missing_junit"}
    try:
        cases = list(ET.parse(path).getroot().iter("testcase"))
    except ET.ParseError:
        return {"passed": False, "tests": 0, "reason": "invalid_junit"}
    failed = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
    skipped = sum(c.find("skipped") is not None for c in cases)
    executed = len(cases) - skipped
    return {
        "passed": failed == 0 and executed >= minimum,
        "tests": len(cases),
        "executed": executed,
        "failed": failed,
        "skipped": skipped,
        "reason": "acceptance_passed"
        if failed == 0 and executed >= minimum
        else "acceptance_failed",
    }


def grade(tree: Path, task: dict, python: str, artifacts: Path, name: str) -> dict:
    report = artifacts / (name + ".xml")
    command = [python, "-m", "pytest", "-q", *task["pytest_args"], f"--junitxml={report}"]
    environment = os.environ.copy()
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        OMNIGENT_DATA_DIR=str(artifacts / (name + "-data")),
        OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
    )
    started = time.monotonic()
    with (artifacts / (name + ".log")).open("wb") as output:
        exit_code, timed_out = bounded_process(
            command,
            cwd=tree,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=task.get("grader_timeout_seconds", 120),
        )
        if timed_out:
            return {
                "passed": False,
                "reason": "grader_timeout",
                "seconds": time.monotonic() - started,
            }
    verdict = junit_result(report, task["minimum_tests"])
    verdict.update(exit_code=exit_code, seconds=time.monotonic() - started)
    verdict["passed"] = verdict["passed"] and exit_code == 0
    return verdict


def cli_metrics(path: Path) -> dict:
    """CLI request configuration is attribution; usage events don't name a model."""
    totals: dict[str, int] = {}
    completed = errors = malformed = tools = 0
    inference_started = False
    availability_rejection = None
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if event.get("type") == "turn.completed":
            completed += 1
            inference_started = True
            for key, value in (event.get("usage") or {}).items():
                if type(value) is int:
                    totals[key] = totals.get(key, 0) + value
        if event.get("type") in {"error", "turn.failed"}:
            errors += 1
            message = str(event.get("message") or event.get("error") or "").lower()
            if "not supported" in message or "unsupported" in message:
                availability_rejection = "unsupported_model_or_configuration"
            elif "unauthorized" in message or "authentication" in message:
                availability_rejection = "authentication_rejected"
        if event.get("type") in {"item.started", "item.completed"}:
            inference_started = True
        if event.get("type") == "item.completed" and event.get("item", {}).get("type") in {
            "command_execution",
            "mcp_tool_call",
            "web_search",
            "file_change",
        }:
            tools += 1
    return {
        "completed_turns": completed,
        "inference_started": inference_started,
        "availability_rejection": availability_rejection,
        "error_events": errors,
        "malformed_events": malformed,
        "tool_or_file_events": tools,
        "usage": totals,
        "model_source": "requested_cli_configuration",
        "actual_model_from_usage": None,
        "spending": None,
        "subscription_quota_delta": None,
    }


def native_command(codex: str, profile: dict, tree: Path, output: Path) -> list[str]:
    return [
        codex,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "-m",
        profile["model"],
        "-c",
        "model_reasoning_effort=" + json.dumps(profile["effort"]),
        "--json",
        "--color",
        "never",
        "-C",
        str(tree),
        "-o",
        str(output),
        "-",
    ]


def evaluate(manifest: dict, destination: Path) -> dict:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    codex, python = manifest["codex"], manifest["python"]
    environment = {
        "codex_version": run([codex, "--version"], destination).stdout.decode().strip(),
        "python_version": run([python, "--version"], destination).stdout.decode().strip(),
        "dependency_fingerprint": manifest["dependency_fingerprint"],
        "sandbox": "workspace-write",
        "approval": "never",
        "user_configuration_loaded": False,
        "harness": "codex-cli-native",
        "access_lane": "existing_codex_sign_in",
    }
    header = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "synthetic": True,
        "score_eligible": False,
        "creator": "routing-paired-eval",
        "environment": environment,
        "manifest_digest": digest(manifest),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (destination / "creation-manifest.json").write_text(
        json.dumps({**header, "manifest": manifest}, indent=2)
    )
    results, validations = [], []
    with (destination / "results.jsonl").open("x") as journal:
        for task in manifest["tasks"]:
            task_dir = destination / task["id"]
            task_dir.mkdir(mode=0o700)
            repo = Path(task["repository"])
            base_sha, base = archive(repo, task["base"])
            reference_sha, reference = archive(repo, task["reference"])
            broken, accepted = task_dir / "broken-validation", task_dir / "reference-validation"
            extract(base, broken)
            extract(reference, accepted)
            verifier_hashes = install_verifiers(
                repo, reference_sha, broken, task["verifier_files"]
            )
            install_verifiers(repo, reference_sha, accepted, task["verifier_files"])
            before = grade(broken, task, python, task_dir, "broken-validation")
            after = grade(accepted, task, python, task_dir, "reference-validation")
            fixture_valid = not before["passed"] and after["passed"]
            validation = {
                "task_id": task["id"],
                "valid": fixture_valid,
                "before": before,
                "reference": after,
            }
            validations.append(validation)
            (task_dir / "fixture-validation.json").write_text(json.dumps(validation, indent=2))
            print(json.dumps({"event": "fixture_validated", **validation}), flush=True)
            if not fixture_valid:
                continue
            fingerprint = digest(
                {
                    "base_archive": hashlib.sha256(base).hexdigest(),
                    "prompt": task["prompt"],
                    "verifiers": verifier_hashes,
                    "pytest_args": task["pytest_args"],
                    "minimum_tests": task["minimum_tests"],
                    "allowed_source_files": task["allowed_source_files"],
                }
            )
            for profile in manifest["profiles"]:
                arm = task_dir / profile["id"]
                arm.mkdir(mode=0o700)
                workspace, grading = arm / "workspace", arm / "grading"
                extract(base, workspace)
                run(["git", "init", "-q"], workspace)
                prompt = (
                    "This is an isolated source-only evaluation fixture. Implement the requested "
                    "behavior here. Do not push, deploy, access other projects, "
                    "or install dependencies. "
                    "Use the existing Python runtime: "
                    + python
                    + ". Only these source files enter "
                    "the independent grader: "
                    + ", ".join(task["allowed_source_files"])
                    + ".\n\n"
                    + task["prompt"]
                )
                (arm / "prompt.txt").write_text(prompt)
                command = native_command(
                    codex, profile, workspace, workspace / "evaluation-answer.txt"
                )
                (arm / "command.json").write_text(json.dumps(command))
                print(
                    json.dumps(
                        {"event": "trial_started", "task": task["id"], "profile": profile["id"]}
                    ),
                    flush=True,
                )
                started = time.monotonic()
                runtime = os.environ.copy()
                runtime.update(
                    PYTHONDONTWRITEBYTECODE="1",
                    OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
                    OMNIGENT_DATA_DIR=str(workspace / ".evaluation-data"),
                )
                with (
                    (arm / "events.jsonl").open("wb") as stdout,
                    (arm / "stderr.log").open("wb") as stderr,
                ):
                    exit_code, timed_out = bounded_process(
                        command,
                        input_bytes=prompt.encode(),
                        stdout=stdout,
                        stderr=stderr,
                        env=runtime,
                        timeout=manifest["budget_seconds"],
                    )
                seconds = time.monotonic() - started
                extract(base, grading)
                install_verifiers(repo, reference_sha, grading, task["verifier_files"])
                submission_error = None
                try:
                    copy_source(workspace, grading, task["allowed_source_files"])
                    verdict = grade(grading, task, python, arm, "acceptance")
                except ValueError as exc:
                    submission_error = str(exc)
                    verdict = {"passed": False, "reason": "invalid_submission"}
                metrics = cli_metrics(arm / "events.jsonl")
                answer = workspace / "evaluation-answer.txt"
                completed = (
                    exit_code == 0
                    and metrics["completed_turns"] > 0
                    and answer.exists()
                    and bool(answer.read_text().strip())
                )
                record = {
                    **header,
                    "task_id": task["id"],
                    "task_group": task["group"],
                    "task_family": task["family"],
                    "task_fingerprint": fingerprint,
                    "base_sha": base_sha,
                    "reference_sha": reference_sha,
                    "profile": profile,
                    "budget_seconds": manifest["budget_seconds"],
                    "verifier_validated": True,
                    "offline_comparison_eligible": metrics["inference_started"]
                    and metrics["availability_rejection"] is None,
                    "verifier_hashes": verifier_hashes,
                    "runtime_completed": completed,
                    "success": completed and verdict["passed"],
                    "seconds": seconds,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "acceptance": verdict,
                    "metrics": metrics,
                    "submission_error": submission_error,
                    "artifact_directory": str(arm),
                }
                journal.write(json.dumps(record) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                results.append(record)
                print(
                    json.dumps(
                        {
                            "event": "trial_finished",
                            "task": task["id"],
                            "profile": profile["id"],
                            "success": record["success"],
                            "seconds": round(seconds, 2),
                            "acceptance": verdict,
                            "metrics": metrics,
                        }
                    ),
                    flush=True,
                )
    summary = {
        **header,
        "fixture_validations": validations,
        "trials": len(results),
        "results_file": str(destination / "results.jsonl"),
    }
    (destination / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.manifest.read_text()), args.destination), indent=2))
