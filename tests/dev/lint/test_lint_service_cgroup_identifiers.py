"""Service identity exemptions must not hide actual model selections."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev.lint.lint_no_hardcoded_models import scan


@pytest.mark.parametrize(
    ("name", "value", "models"),
    [
        ("_O1_SERVICE_CGROUP", "omnigent-o1", []),
        ("_O2_SERVICE_CGROUP", "omnigent-o2", []),
        ("_O1_SERVICE_CGROUP", "o1", ["o1"]),
        ("_O2_SERVICE_CGROUP", "o3", ["o3"]),
        ("_O1_SERVICE_CGROUP", "gpt-5.5", ["gpt-5.5"]),
        ("_O2_SERVICE_CGROUP", "omnigent-o1", ["o1"]),
        ("_O1_SERVICE_CGROUP", "omnigent-o1 gpt-5.5", ["o1", "gpt-5.5"]),
        ("MODEL", "o1", ["o1"]),
        ("MODEL", "gpt-5.5", ["gpt-5.5"]),
    ],
)
def test_service_cgroup_exemption_requires_exact_identity(
    tmp_path: Path, name: str, value: str, models: list[str]
) -> None:
    source = tmp_path / "config.py"
    source.write_text(f"{name} = {value!r}\n")
    assert [hit.model for hit in scan(source)] == models


def test_service_cgroup_and_real_model_in_the_same_file(tmp_path: Path) -> None:
    source = tmp_path / "config.py"
    source.write_text('_O1_SERVICE_CGROUP = "omnigent-o1"\nMODEL = "o1"\n')
    assert [(hit.line, hit.model) for hit in scan(source)] == [(2, "o1")]
