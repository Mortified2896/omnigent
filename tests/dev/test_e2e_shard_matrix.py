"""Check the shared shard producer and the draft-PR matrix expansion guard."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("bash") is None, reason="CI shard producer requires Bash")
@pytest.mark.parametrize("shards", [4, 5])
@pytest.mark.parametrize(
    ("event", "draft"),
    [
        ("pull_request", "true"),
        ("pull_request", "false"),
        ("workflow_dispatch", ""),
        ("schedule", ""),
    ],
)
def test_shard_matrix_output(tmp_path: Path, shards: int, event: str, draft: str) -> None:
    output = tmp_path / "github-output"
    subprocess.run(
        ["bash", str(ROOT / ".github/scripts/ci/e2e-shard-matrix.sh")],
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output),
            "EVENT_NAME": event,
            "IS_DRAFT": draft,
            "NUM_SHARDS": str(shards),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    key, separator, value = output.read_text().strip().partition("=")
    assert key == "matrix" and separator == "="
    expected = [{"shard_id": index, "num_shards": shards} for index in range(shards)]
    if draft == "true":
        expected = []
    assert json.loads(value) == {"include": expected}


@pytest.mark.parametrize(
    ("filename", "job_name"),
    [("e2e.yml", "e2e"), ("integration.yml", "integration")],
)
def test_e2e_skips_empty_matrix_before_strategy_expansion(filename: str, job_name: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
    job = workflow["jobs"][job_name]
    assert job["if"] == "needs.setup.outputs.matrix != '{\"include\":[]}'"
    assert job["strategy"]["matrix"] == "${{ fromJSON(needs.setup.outputs.matrix) }}"


@pytest.mark.parametrize(
    ("filename", "job_name"),
    [("e2e.yml", "e2e"), ("integration.yml", "integration")],
)
def test_e2e_keeps_security_gate_dependency(filename: str, job_name: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
    jobs = workflow["jobs"]
    assert jobs[job_name]["needs"] == "setup"
    assert jobs["setup"]["needs"] == "gate"
    assert jobs["gate"]["uses"] == "./.github/workflows/security-gate.yml"


@pytest.mark.parametrize("draft", ["true", "false", ""])
def test_integration_matrix_output(tmp_path: Path, draft: str) -> None:
    output = tmp_path / "github-output"
    subprocess.run(
        ["bash", str(ROOT / ".github/scripts/ci/integration-matrix.sh")],
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output),
            "EVENT_NAME": "pull_request" if draft else "workflow_dispatch",
            "IS_DRAFT": draft,
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    key, separator, value = output.read_text().strip().partition("=")
    assert key == "matrix" and separator == "="
    expected = [
        {"name": "openai-agents", "harness": "openai-agents", "model": "mock-model", "workers": 4}
    ]
    assert json.loads(value) == {"include": [] if draft == "true" else expected}
