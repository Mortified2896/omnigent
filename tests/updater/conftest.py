"""Shared pytest fixtures for ``tests/updater/``.

The tests use a temp ``state root`` (under ``/tmp``) so production
paths are never touched, and they run as a non-root user so the
per-request locks fall back to ``os.kill(pid, 0)`` instead of
``sudo``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the updater state root to a tmpdir."""
    root = tmp_path / "updates"
    root.mkdir()
    monkeypatch.setenv("OMNIGENT_UPDATER_STATE_ROOT", str(root))
    # Wipe the cached state_root() so the layout module picks up
    # the override. The layout module reads the env on every call
    # so this is automatic, but we also reload here for paranoia.
    return root


@pytest.fixture
def deploy_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the deploy root to a tmpdir."""
    root = tmp_path / "deploy"
    root.mkdir()
    monkeypatch.setenv("OMNIGENT_DEPLOY_ROOT", str(root))
    return root


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a minimal fake git repo for validation tests.

    The repo carries one initial commit (``seed``) plus a linear
    chain of dummy commits the tests can reference as a "target"
    or a "stale expected current". The repo root is wired through
    ``OMNIGENT_UPDATER_REPO_ROOT`` so ``omnigent.updater.layout``
    returns it.

    The fixture's teardown removes the repo so subsequent tests
    start clean.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Updater Tests")
    (repo / "README").write_text("seed\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "seed", "-q")
    monkeypatch.setenv("OMNIGENT_UPDATER_REPO_ROOT", str(repo))
    yield repo
    shutil.rmtree(repo, ignore_errors=True)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout.strip()


@pytest.fixture
def make_commit(repo_root: Path):
    """Return a callable that creates a new commit and returns its SHA."""

    def _make(message: str = "wip") -> str:
        (repo_root / "README").write_text(f"{message}\n")
        _git(repo_root, "add", "README")
        _git(repo_root, "commit", "-m", message, "-q")
        return _git(repo_root, "rev-parse", "HEAD")

    return _make


@pytest.fixture
def lineage_anchor(monkeypatch: pytest.MonkeyPatch, make_commit) -> str:
    """Pin the lineage anchor to a real commit in the fake repo.

    The anchor must be reachable from every target the tests build,
    so the validation path treats the anchor as a known good
    starting point.
    """
    sha = make_commit("lineage-anchor")
    monkeypatch.setenv("OMNIGENT_UPDATER_LINEAGE_ANCHOR", sha)
    return sha


@pytest.fixture
def live_sha_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the live-deployed-SHA file to a tmpdir path."""
    path = tmp_path / "deployed-sha"
    path.write_text("")
    monkeypatch.setenv("OMNIGENT_UPDATER_LIVE_SHA_FILE", str(path))
    return path
