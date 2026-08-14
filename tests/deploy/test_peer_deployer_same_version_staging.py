"""Real-pip regressions for copied same-version peer staging."""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from deploy.scripts.peer_deployer import identity, staging

VERSION = "0.9.0.dev0"
RUNTIME_SHA = "a" * 40
PACKAGE_SPECS = (
    ("main", "omnigent", "omnigent"),
    ("sdk_client", "omnigent-client", "omnigent_client"),
    ("sdk_ui", "omnigent-ui-sdk", "omnigent_ui_sdk"),
)


def _record_hash(payload: bytes) -> str:
    digest = hashlib.sha256(payload).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _write_test_wheel(
    root: Path,
    *,
    distribution: str,
    module: str,
    marker: str,
    include_omnigent_entry_point: bool = True,
) -> Path:
    """Write a minimal wheel that is installed by real pip during the test."""
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{VERSION}.dist-info"
    wheel = root / f"{normalized}-{VERSION}-py3-none-any.whl"
    files: dict[str, bytes] = {
        f"{module}/__init__.py": f"ARTIFACT_MARKER = {marker!r}\n".encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {VERSION}\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: peer-staging-regression\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    if module == "omnigent":
        files[f"{module}/_build_info.py"] = f"COMMIT_SHA = {RUNTIME_SHA!r}\n".encode()
        files[f"{module}/cli.py"] = b"def main():\n    print('synthetic omnigent help')\n"
        if include_omnigent_entry_point:
            files[f"{dist_info}/entry_points.txt"] = (
                b"[console_scripts]\nomnigent = omnigent.cli:main\nomni = omnigent.cli:main\n"
            )
    record_lines = [
        f"{name},sha256={_record_hash(payload)},{len(payload)}" for name, payload in files.items()
    ]
    record_lines.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(record_lines) + "\n").encode()
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return wheel


def _wheel_set(
    root: Path,
    *,
    marker: str,
    main_has_entry_point: bool = True,
) -> dict[str, Path]:
    return {
        role: _write_test_wheel(
            root,
            distribution=distribution,
            module=module,
            marker=marker,
            include_omnigent_entry_point=(main_has_entry_point if role == "main" else True),
        )
        for role, distribution, module in PACKAGE_SPECS
    }


def _make_installed_supervisor(
    root: Path,
    *,
    candidate_has_entry_point: bool = True,
) -> tuple[identity.Instance, Path, dict[str, Path]]:
    """Install old wheels in a real venv and return equal-version replacements."""
    release_root = root / "supervisor" / "releases" / RUNTIME_SHA
    release_root.mkdir(parents=True)
    (release_root / "PROVENANCE.txt").write_text(f"sha={RUNTIME_SHA}\npackage_version={VERSION}\n")
    supervisor_venv = release_root / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(supervisor_venv)],
        check=True,
        capture_output=True,
    )
    old_wheels = _wheel_set(root / "old-wheels", marker="supervisor")
    subprocess.run(
        [
            str(supervisor_venv / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            *map(str, old_wheels.values()),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (supervisor_venv / "bin" / "omnigent").is_file()

    deploy_root = root / "supervisor"
    (deploy_root / "current").symlink_to(release_root)
    supervisor = identity.Instance(
        name="O2-fake",
        deployment_root=deploy_root,
        service_unit="x.service",
        host_unit="x-host.service",
        port=4197,
        health_url="http://127.0.0.1:4197/health",
    )
    replacements = _wheel_set(
        root / "candidate-wheels",
        marker="candidate",
        main_has_entry_point=candidate_has_entry_point,
    )
    return supervisor, release_root, replacements


def test_same_version_artifacts_are_reinstalled_and_recreate_console_script(
    tmp_path: Path,
) -> None:
    """Copied equal-version metadata cannot make pip skip artifact replacement."""
    supervisor, supervisor_release, wheels = _make_installed_supervisor(tmp_path)
    target_release = tmp_path / "candidate"

    staging.stage_candidate_runtime(
        target_release,
        supervisor,
        wheels,
        supervisor_release_root=supervisor_release,
    )

    candidate_python = target_release / "venv" / "bin" / "python"
    candidate_entry_point = target_release / "venv" / "bin" / "omnigent"
    marker = subprocess.run(
        [str(candidate_python), "-c", "import omnigent; print(omnigent.ARTIFACT_MARKER)"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert marker.stdout.strip() == "candidate"
    assert candidate_entry_point.is_file()
    assert os.access(candidate_entry_point, os.X_OK)
    assert str(candidate_python).encode() in candidate_entry_point.read_bytes()[:4096]
    help_result = subprocess.run(
        [str(candidate_entry_point), "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert help_result.stdout.strip() == "synthetic omnigent help"
    assert (target_release / ".complete").is_file()


def test_missing_candidate_console_script_fails_staging_closed(tmp_path: Path) -> None:
    """A wheel without the required launcher aborts and removes partial staging."""
    supervisor, supervisor_release, wheels = _make_installed_supervisor(
        tmp_path,
        candidate_has_entry_point=False,
    )
    target_release = tmp_path / "candidate"

    with pytest.raises(staging.StagingError, match="omnigent console entry point"):
        staging.stage_candidate_runtime(
            target_release,
            supervisor,
            wheels,
            supervisor_release_root=supervisor_release,
        )

    assert not target_release.exists()


def test_relocated_candidate_launchers_are_rebound_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real pip launcher is broken by rename, then rebound at the final path."""
    supervisor, supervisor_release, wheels = _make_installed_supervisor(tmp_path)
    staging_root = tmp_path / "target" / "staging" / "promotion-relocation-test"
    final_root = tmp_path / "target" / "releases" / RUNTIME_SHA
    staging.stage_candidate_runtime(
        staging_root,
        supervisor,
        wheels,
        supervisor_release_root=supervisor_release,
    )
    staged_python = staging_root / "venv" / "bin" / "python"
    staged_launcher = staging_root / "venv" / "bin" / "omnigent"
    assert str(staged_python).encode() in staged_launcher.read_bytes()[:4096]

    final_root.parent.mkdir(parents=True)
    os.rename(staging_root, final_root)
    final_launcher = final_root / "venv" / "bin" / "omnigent"
    with pytest.raises(FileNotFoundError):
        subprocess.run([str(final_launcher), "--help"], check=False)

    commands: list[list[str]] = []
    real_run = subprocess.run

    def record_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(staging.subprocess, "run", record_run)
    staging.finalize_relocated_runtime(
        final_root,
        final_root / "artifacts" / wheels["main"].name,
        staging_root=staging_root,
    )

    install = next(argv for argv in commands if "install" in argv and "pip" in argv)
    assert "--force-reinstall" in install
    assert "--no-deps" in install
    assert "--no-index" in install
    assert [argument for argument in install if argument.endswith(".whl")] == [
        str(final_root / "artifacts" / wheels["main"].name)
    ]

    final_python = final_root / "venv" / "bin" / "python"
    for name in ("omnigent", "omni"):
        launcher = final_root / "venv" / "bin" / name
        payload = launcher.read_bytes()[:4096]
        assert str(final_python).encode() in payload
        assert str(staging_root).encode() not in payload
        assert subprocess.run([str(launcher), "--help"], check=False).returncode == 0
    identity_blob = identity.runtime_identity(final_python)
    assert identity_blob == {"commit_sha": RUNTIME_SHA, "version": VERSION}
