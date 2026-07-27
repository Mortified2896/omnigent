"""Tests for the release-layout helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.deploy.ops import layout


@pytest.fixture
def deploy_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override the deploy root to a tmpdir so tests don't pollute /home."""
    monkeypatch.setenv("OMNIGENT_DEPLOY_ROOT", str(tmp_path))
    return tmp_path


def test_deploy_root_lazy_creates(deploy_root: Path) -> None:
    """``deploy_root()`` creates the root on first call."""
    assert layout.deploy_root() == deploy_root
    assert deploy_root.is_dir()


def test_releases_dir_under_root(deploy_root: Path) -> None:
    # The layout module lazy-creates subdirs on first access; invoke
    # the helpers first so the assertion is on a directory that exists.
    layout.releases_dir()
    layout.manifests_dir()
    layout.failed_dir()
    assert layout.releases_dir() == deploy_root / "releases"
    assert layout.manifests_dir() == deploy_root / "manifests"
    assert layout.failed_dir() == deploy_root / "failed"


def test_release_dir_for_uses_full_sha(deploy_root: Path) -> None:
    """Release dirs always keyed by full 40-character SHA."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert layout.release_dir_for(sha) == deploy_root / "releases" / sha


def test_manifest_path_for_sha_archived_under_manifests(deploy_root: Path) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert layout.manifest_path_for_sha(sha) == deploy_root / "manifests" / f"{sha}.json"


def test_failed_dir_for_each_sha_isolated(deploy_root: Path) -> None:
    """Two failed SHAs get two separate dirs so diagnostics don't cross."""
    sha_a = "0000000000000000000000000000000000000000"
    sha_b = "1111111111111111111111111111111111111111"
    assert layout.failed_dir_for(sha_a) != layout.failed_dir_for(sha_b)


def test_safe_resolve_rejects_escape_attempt(deploy_root: Path) -> None:
    """``safe_resolve`` refuses to follow symlinks that leave the deploy root."""
    (deploy_root / "evil").parent.mkdir(parents=True, exist_ok=True)
    escape_target = deploy_root.parent / "evil-escape"
    escape_target.mkdir()
    symlink = deploy_root / "evil"
    os.symlink(escape_target, symlink)
    with pytest.raises(ValueError) as exc:
        layout.safe_resolve(symlink)
    assert "outside deploy root" in str(exc.value).lower()


def test_safe_resolve_accepts_inside_root(deploy_root: Path) -> None:
    """Paths inside the deploy root resolve cleanly."""
    inside = deploy_root / "releases" / "abc"
    inside.mkdir(parents=True)
    assert layout.safe_resolve(inside) == inside.resolve()


def test_default_deploy_root_is_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``OMNIGENT_DEPLOY_ROOT`` is unset, the home default applies.

    Skipped when the default path's parent isn't writable on the host
    (CI may not have permission to mkdir ``/home/...``). The host
    where this matters is documented in :mod:`omnigent.deploy.ops.layout`.
    """
    monkeypatch.delenv("OMNIGENT_DEPLOY_ROOT", raising=False)
    default = Path("/home/hermes/workspace/deployments/omnigent")
    try:
        assert layout.deploy_root() == default
    except (FileExistsError, PermissionError, OSError):
        pytest.skip("cannot create the default deploy root on this host")
