"""Focused tests for the Control Room immutable-release preflight."""

from __future__ import annotations

import importlib.util
import stat
import zipfile
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "scripts"
    / "omnigent_release_preflight.py"
)
_SPEC = importlib.util.spec_from_file_location("control_room_release_preflight", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)

_SHA = "a" * 40


def _write_test_wheel(tmp_path: Path, *, embedded_sha: str = _SHA) -> Path:
    wheel = tmp_path / "omnigent-0.9.0.dev0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "omnigent/_build_info.py",
            f"COMMIT_SHA: str = {embedded_sha!r}\nBUILD_TIME_EPOCH: int = 1\n",
        )
        archive.writestr(
            "omnigent-0.9.0.dev0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: omnigent\nVersion: 0.9.0.dev0\n",
        )
        prefix = "omnigent/server/static/web-ui/"
        archive.writestr(
            f"{prefix}index.html",
            '<html><head><title>Omnigent</title>'
            '<link href="/assets/app.css" rel="stylesheet"></head>'
            '<body><script src="/assets/app.js"></script></body></html>',
        )
        archive.writestr(f"{prefix}version.json", "{}")
        archive.writestr(f"{prefix}manifest.webmanifest", "{}")
        archive.writestr(f"{prefix}assets/app.js", "console.log('ok')")
        archive.writestr(f"{prefix}assets/app.css", "body{}")
    return wheel


def test_require_sha_accepts_only_exact_lowercase_sha() -> None:
    assert preflight.require_sha(_SHA) == _SHA
    for invalid in ("a" * 39, "a" * 41, "A" * 40, "main", ""):
        with pytest.raises(preflight.PreflightError):
            preflight.require_sha(invalid)


def test_inspect_wheel_proves_embedded_sha_metadata_and_spa(tmp_path: Path) -> None:
    wheel = _write_test_wheel(tmp_path)

    result = preflight.inspect_wheel(wheel, _SHA)

    assert result["sha"] == _SHA
    assert result["package_version"] == "0.9.0.dev0"
    assert result["wheel_filename"] == wheel.name
    assert len(result["wheel_sha256"]) == 64


def test_inspect_wheel_rejects_wrong_embedded_commit(tmp_path: Path) -> None:
    wheel = _write_test_wheel(tmp_path, embedded_sha="b" * 40)

    with pytest.raises(preflight.PreflightError, match="does not match requested SHA"):
        preflight.inspect_wheel(wheel, _SHA)


def test_inspect_wheel_rejects_incomplete_spa(tmp_path: Path) -> None:
    wheel = tmp_path / "omnigent-0.9.0.dev0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("omnigent/_build_info.py", f"COMMIT_SHA = {_SHA!r}\n")
        archive.writestr(
            "omnigent-0.9.0.dev0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: omnigent\nVersion: 0.9.0.dev0\n",
        )
        archive.writestr(
            "omnigent/server/static/web-ui/index.html",
            "<html><head><title>Omnigent</title></head></html>",
        )

    with pytest.raises(preflight.PreflightError, match="SPA bundle is incomplete"):
        preflight.inspect_wheel(wheel, _SHA)


def test_installed_identity_rejects_missing_console_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = tmp_path / _SHA
    bin_dir = release / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("#!/usr/bin/env python\n")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(preflight.PreflightError, match="missing python or omnigent"):
        preflight._inspect_installed_identity(release)


def test_installed_identity_rejects_missing_entrypoint_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = tmp_path / _SHA
    bin_dir = release / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("#!/usr/bin/env python\n")
    python.chmod(0o755)
    cli = bin_dir / "omnigent"
    cli.write_text(f"#!{tmp_path}/missing-python\n")
    cli.chmod(0o755)

    with pytest.raises(preflight.PreflightError, match="interpreter does not exist"):
        preflight._inspect_installed_identity(release)


def test_read_provenance_rejects_duplicate_or_malformed_fields(tmp_path: Path) -> None:
    path = tmp_path / "PROVENANCE.txt"
    path.write_text(
        "schema_version=1\n"
        f"sha={_SHA}\n"
        "package_version=0.9.0.dev0\n"
        f"wheel_sha256={'0' * 64}\n"
        "wheel_filename=omnigent.whl\n"
        "built_at_utc=2026-08-07T00:00:00Z\n"
        "builder=test\n"
        "builder=duplicate\n"
    )

    with pytest.raises(preflight.PreflightError, match="invalid provenance line"):
        preflight.read_provenance(path)
