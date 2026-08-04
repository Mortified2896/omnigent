from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

if repo_override := os.environ.get("OMNIGENT_TEST_REPO_ROOT"):
    REPO_ROOT = Path(repo_override)
else:
    REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_PATH = REPO_ROOT / "deploy/scripts/omnigent_release_preflight.py"
PROD_SCRIPT = REPO_ROOT / "deploy/scripts/deploy-omnigent-production.sh"
MAINT_SCRIPT = REPO_ROOT / "deploy/scripts/promote-omnigent-maintenance.sh"
SHA = "a" * 40

spec = importlib.util.spec_from_file_location("omnigent_release_preflight", PREFLIGHT_PATH)
assert spec and spec.loader
preflight = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = preflight
spec.loader.exec_module(preflight)


def make_wheel(tmp_path: Path, *, sha: str = SHA, referenced_asset: bool = True) -> Path:
    wheel = tmp_path / "omnigent-1.2.3-py3-none-any.whl"
    index = (
        '<!doctype html><title>Omnigent</title>'
        '<link rel="manifest" href="/manifest.webmanifest">'
        '<script type="module" src="/assets/index.js"></script>'
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "omnigent/_build_info.py",
            f"COMMIT_SHA: str = {sha!r}\nBUILD_TIME_EPOCH: int = 1\n",
        )
        archive.writestr(
            "omnigent-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: omnigent\nVersion: 1.2.3\n",
        )
        prefix = "omnigent/server/static/web-ui/"
        archive.writestr(prefix + "index.html", index)
        archive.writestr(prefix + "version.json", "{}")
        archive.writestr(prefix + "manifest.webmanifest", "{}")
        if referenced_asset:
            archive.writestr(prefix + "assets/index.js", "export {}")
    return wheel


def test_wheel_preflight_accepts_exact_sha_and_complete_spa(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path)
    result = preflight.inspect_wheel(wheel, SHA)
    assert result["sha"] == SHA
    assert result["package_version"] == "1.2.3"
    assert len(result["wheel_sha256"]) == 64


def test_wheel_preflight_rejects_mismatched_build_sha(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path, sha="b" * 40)
    with pytest.raises(preflight.PreflightError, match="does not match requested SHA"):
        preflight.inspect_wheel(wheel, SHA)


def test_wheel_preflight_rejects_incomplete_spa(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path, referenced_asset=False)
    with pytest.raises(preflight.PreflightError, match="references missing files"):
        preflight.inspect_wheel(wheel, SHA)


def test_provenance_requires_durable_identity_fields(tmp_path: Path) -> None:
    provenance = tmp_path / "PROVENANCE.txt"
    provenance.write_text(f"schema_version=1\nsha={SHA}\n")
    with pytest.raises(preflight.PreflightError, match="missing fields"):
        preflight.read_provenance(provenance)


@pytest.mark.parametrize("script", [PROD_SCRIPT, MAINT_SCRIPT])
def test_deploy_scripts_are_valid_bash(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", [PROD_SCRIPT, MAINT_SCRIPT])
def test_deploy_scripts_reject_web_ui_skip_even_when_false(script: Path) -> None:
    result = subprocess.run(
        [str(script), "--help"],
        env={**os.environ, "OMNIGENT_SKIP_WEB_UI": "false"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "OMNIGENT_SKIP_WEB_UI must be unset" in result.stderr


def test_production_writes_exact_candidate_only_after_acceptance() -> None:
    text = PROD_SCRIPT.read_text()
    acceptance = text.index('run_acceptance "$release_dir" "$sha"')
    marker = text.index('write_marker "$sha"', acceptance)
    assert marker > acceptance
    assert 'sha="wheel-' not in text
    assert "refusing unsafe reuse" in text


def test_maintenance_consumes_exact_production_marker_and_has_rollback() -> None:
    text = MAINT_SCRIPT.read_text()
    assert 'candidate=$(<"$PROD_MARKER")' in text
    assert '[[ "$sha" == "$candidate" ]]' in text
    assert 'current_prod=$(readlink -f "$PROD_ROOT/current"' in text
    assert "restore_previous" in text
    assert "--rollback" in text
