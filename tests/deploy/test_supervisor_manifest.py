"""Tests for the release manifest module."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from omnigent.deploy.supervisor.manifest import (
    ManifestError,
    ReleaseManifest,
    load_manifest,
    manifest_path_for,
    verify_manifest_commit,
    write_manifest,
)


@pytest.fixture
def sample_manifest(tmp_path: Path) -> ReleaseManifest:
    sha = "0123456789abcdef0123456789abcdef01234567"
    (tmp_path / "uv.lock").write_text("# uv lock\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package-lock.json").write_text("{}\n")
    return ReleaseManifest(
        commit_sha=sha,
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir=str(tmp_path),
        python_executable=str(tmp_path / ".venv" / "bin" / "python"),
        python_version="3.12.13",
        omnigent_module_path=str(tmp_path / "omnigent" / "__init__.py"),
        omnigent_server_app_path=str(tmp_path / "omnigent" / "server" / "app.py"),
        frontend_build_version="01234567",
        lockfile_hashes={
            "uv.lock": _sha256(tmp_path / "uv.lock"),
            "web/package-lock.json": _sha256(tmp_path / "web" / "package-lock.json"),
        },
    )


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_manifest_round_trip(tmp_path: Path, sample_manifest: ReleaseManifest) -> None:
    """Write then load returns the same dataclass."""
    path = write_manifest(tmp_path, sample_manifest)
    assert path == manifest_path_for(tmp_path)

    loaded = load_manifest(tmp_path)
    assert loaded.commit_sha == sample_manifest.commit_sha
    assert loaded.frontend_build_version == sample_manifest.frontend_build_version
    assert loaded.lockfile_hashes == sample_manifest.lockfile_hashes


def test_write_manifest_refuses_overwrite(
    tmp_path: Path, sample_manifest: ReleaseManifest
) -> None:
    """Refuse to overwrite an existing manifest without explicit opt-in."""
    write_manifest(tmp_path, sample_manifest)
    with pytest.raises(ManifestError) as exc:
        write_manifest(tmp_path, sample_manifest)
    assert "refusing to overwrite" in str(exc.value).lower()


def test_write_manifest_overwrite_with_opt_in(
    tmp_path: Path, sample_manifest: ReleaseManifest
) -> None:
    """``OMNIGENT_DEPLOY_ALLOW_MANIFEST_OVERWRITE=1`` permits re-write."""
    write_manifest(tmp_path, sample_manifest)
    os.environ["OMNIGENT_DEPLOY_ALLOW_MANIFEST_OVERWRITE"] = "1"
    try:
        path = write_manifest(tmp_path, sample_manifest)
    finally:
        del os.environ["OMNIGENT_DEPLOY_ALLOW_MANIFEST_OVERWRITE"]
    assert path.is_file()


def test_load_manifest_missing(tmp_path: Path) -> None:
    """Missing manifest raises ManifestError, not FileNotFoundError."""
    with pytest.raises(ManifestError) as exc:
        load_manifest(tmp_path)
    assert "missing" in str(exc.value).lower()


def test_load_manifest_malformed(tmp_path: Path) -> None:
    """JSON garbage is rejected with a clear error."""
    path = manifest_path_for(tmp_path)
    path.write_text("not-json")
    with pytest.raises(ManifestError) as exc:
        load_manifest(tmp_path)
    assert "json" in str(exc.value).lower()


def test_load_manifest_missing_fields(tmp_path: Path) -> None:
    """A JSON without ``commit_sha`` is rejected."""
    path = manifest_path_for(tmp_path)
    path.write_text(json.dumps({"built_at": "2026-07-26T00:00:00Z"}))
    with pytest.raises(ManifestError) as exc:
        load_manifest(tmp_path)
    assert "missing fields" in str(exc.value).lower()


def test_verify_manifest_commit_matches() -> None:
    """A matching SHA passes."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    manifest = ReleaseManifest(
        commit_sha=sha,
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir="/tmp",
        python_executable="/tmp/.venv/bin/python",
        python_version="3.12.13",
        omnigent_module_path="/tmp/omnigent/__init__.py",
        omnigent_server_app_path="/tmp/omnigent/server/app.py",
    )
    verify_manifest_commit(manifest, sha)


def test_verify_manifest_commit_rejects_mismatch() -> None:
    """A mismatched SHA raises ManifestError."""
    manifest = ReleaseManifest(
        commit_sha="0000000000000000000000000000000000000000",
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir="/tmp",
        python_executable="/tmp/.venv/bin/python",
        python_version="3.12.13",
        omnigent_module_path="/tmp/omnigent/__init__.py",
        omnigent_server_app_path="/tmp/omnigent/server/app.py",
    )
    with pytest.raises(ManifestError) as exc:
        verify_manifest_commit(manifest, "1111111111111111111111111111111111111111")
    assert "does not match" in str(exc.value)


def test_verify_manifest_commit_rejects_empty_expected() -> None:
    """An empty expected SHA is rejected."""
    manifest = ReleaseManifest(
        commit_sha="0000000000000000000000000000000000000000",
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir="/tmp",
        python_executable="/tmp/.venv/bin/python",
        python_version="3.12.13",
        omnigent_module_path="/tmp/omnigent/__init__.py",
        omnigent_server_app_path="/tmp/omnigent/server/app.py",
    )
    with pytest.raises(ManifestError) as exc:
        verify_manifest_commit(manifest, "")
    assert "empty" in str(exc.value)


# --- Issue #28: manifest identity comes from the caller, not the checkout --


def _basic_release_dir(tmp_path: Path) -> Path:
    """Populate ``tmp_path`` with the files ``from_directory`` inspects."""
    (tmp_path / "uv.lock").write_text("# uv lock\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='omnigent'\n")
    web = tmp_path / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}\n")
    return tmp_path


def test_from_directory_uses_supplied_commit_sha(tmp_path: Path) -> None:
    """``from_directory`` records the explicit SHA the caller passed in.

    The release identity must come from the already-resolved canonical
    SHA, not from any property of the archive directory itself.
    """
    sha = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    manifest = ReleaseManifest.from_directory(
        tmp_path,
        commit_sha=sha,
        repository="Mortified2896/omnigent",
        python_executable="/tmp/.venv/bin/python",
        python_version="3.12.13",
        omnigent_module_path="/tmp/omnigent/__init__.py",
        omnigent_server_app_path="/tmp/omnigent/server/app.py",
        frontend_build_version="build-1",
    )
    assert manifest.commit_sha == sha


def test_from_directory_does_not_require_dot_git(tmp_path: Path) -> None:
    """``from_directory`` works on a release directory with no ``.git``.

    The release archive is built from ``git archive`` and intentionally
    does not contain a git checkout. The manifest must build without
    requiring one.
    """
    _basic_release_dir(tmp_path)
    assert not (tmp_path / ".git").exists()
    ReleaseManifest.from_directory(
        tmp_path,
        commit_sha="abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        repository="Mortified2896/omnigent",
        python_executable="/tmp/.venv/bin/python",
        python_version="3.12.13",
        omnigent_module_path="/tmp/omnigent/__init__.py",
        omnigent_server_app_path="/tmp/omnigent/server/app.py",
    )


def test_from_directory_ignores_unrelated_checkout(tmp_path: Path, monkeypatch) -> None:
    """The archive's git state cannot influence the manifest SHA.

    If a ``.git`` symlink happens to be present and points at a
    different commit, the manifest must still record the SHA the
    caller supplied. We simulate this by stubbing ``subprocess.run`` to
    raise: ``from_directory`` must never call it.
    """
    _basic_release_dir(tmp_path)

    def _explode(*args, **kwargs):
        raise AssertionError(
            "from_directory must not invoke subprocess; identity is supplied by the caller."
        )

    monkeypatch.setattr("subprocess.run", _explode)
    ReleaseManifest.from_directory(
        tmp_path,
        commit_sha="abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        repository="Mortified2896/omnigent",
        python_executable="/tmp/.venv/bin/python",
        python_version="3.12.13",
        omnigent_module_path="/tmp/omnigent/__init__.py",
        omnigent_server_app_path="/tmp/omnigent/server/app.py",
    )


def test_from_directory_records_lockfile_hashes(tmp_path: Path) -> None:
    """Lockfile hashes are collected from the archive."""
    _basic_release_dir(tmp_path)
    lockfile = tmp_path / "web" / "package-lock.json"
    lockfile.write_text('{"name":"web"}\n')
    manifest = ReleaseManifest.from_directory(
        tmp_path,
        commit_sha="abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        repository="Mortified2896/omnigent",
        python_executable="/tmp/.venv/bin/python",
        python_version="3.12.13",
        omnigent_module_path="/tmp/omnigent/__init__.py",
        omnigent_server_app_path="/tmp/omnigent/server/app.py",
    )
    assert "web/package-lock.json" in manifest.lockfile_hashes
    assert (
        manifest.lockfile_hashes["web/package-lock.json"]
        == hashlib.sha256(lockfile.read_bytes()).hexdigest()
    )
