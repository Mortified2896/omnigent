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


def test_e2e_skips_empty_matrix_before_strategy_expansion() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/e2e.yml").read_text())
    job = workflow["jobs"]["e2e"]
    assert job["if"] == "needs.setup.outputs.matrix != '{\"include\":[]}'"
    assert job["strategy"]["matrix"] == "${{ fromJSON(needs.setup.outputs.matrix) }}"


def test_e2e_keeps_security_gate_dependency() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/e2e.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["e2e"]["needs"] == "setup"
    assert jobs["setup"]["needs"] == "gate"
    assert jobs["gate"]["uses"] == "./.github/workflows/security-gate.yml"
