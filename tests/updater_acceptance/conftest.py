"""Staging acceptance fixtures for the external updater (issue #38 §11).

The acceptance suite proves the controller works end-to-end against
an isolated staging deploy root, state root, and live database copy.
It deliberately avoids touching ``/home/hermes/workspace/deployments/omnigent``
and ``/var/lib/omnigent/updates`` so a CI run can never collide with
production paths.

Each fixture pins a separate tmpdir for every piece of the staging
layout:

* staging deploy root (releases/, manifests/, failed/, current,
  previous);
* staging state root (requests/, running/, results/, events/,
  locks/, maintenance.json, backups/, rehearsal/, delivery-ack/,
  pending-deliveries/, cancel-requests.jsonl);
* staging live database (sqlite file);
* staging live deployed-sha file;
* staging git repo (with one initial commit + the lineage anchor +
  one candidate commit);
* staging lineage anchor env var.

The acceptance tests then drive the controller through its full
lifecycle with stubbed subprocess hooks so the suite can prove the
state-machine + delivery + rollback semantics without depending on
a real systemd / real curl / real web service.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def staging_root(tmp_path: Path) -> Path:
    """A top-level staging tmpdir; tests build sub-trees under here."""
    root = tmp_path / "staging"
    root.mkdir()
    return root


@pytest.fixture
def staging_deploy_root(staging_root: Path) -> Path:
    """Isolated deploy root under the staging tmpdir."""
    deploy = staging_root / "deploy"
    deploy.mkdir()
    return deploy


@pytest.fixture
def staging_state_root(staging_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated state root with ``OMNIGENT_UPDATER_STATE_ROOT`` wired up."""
    state = staging_root / "state"
    state.mkdir()
    monkeypatch.setenv("OMNIGENT_UPDATER_STATE_ROOT", str(state))
    return state


@pytest.fixture
def staging_repo(staging_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway git repo with one initial commit.

    Tests add the lineage anchor and candidate commits on top of
    this. ``OMNIGENT_UPDATER_REPO_ROOT`` is wired through so the
    controller uses this repo, not the live checkout.
    """
    repo = staging_root / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main", "-q")
    _git(repo, "config", "user.email", "staging@example.invalid")
    _git(repo, "config", "user.name", "Staging Tests")
    (repo / "README").write_text("seed\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "seed", "-q")
    monkeypatch.setenv("OMNIGENT_UPDATER_REPO_ROOT", str(repo))
    return repo


@pytest.fixture
def staging_live_sha(staging_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The staging ``deployed-sha`` file (mirrors ``~/.omnigent/deployed-sha``)."""
    path = staging_root / "deployed-sha"
    path.write_text("")
    monkeypatch.setenv("OMNIGENT_UPDATER_LIVE_SHA_FILE", str(path))
    return path


@pytest.fixture
def staging_lineage_anchor(monkeypatch: pytest.MonkeyPatch, staging_repo: Path) -> str:
    """Pin the lineage anchor to a real commit in the staging repo."""
    sha = _git_make_commit(staging_repo, "lineage-anchor")
    monkeypatch.setenv("OMNIGENT_UPDATER_LINEAGE_ANCHOR", sha)
    return sha


@pytest.fixture
def staging_db(staging_root: Path) -> Path:
    """The staging SQLite database file."""
    db = staging_root / "chat.db"
    db.write_text("")
    return db


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def _git_make_commit(repo: Path, message: str) -> str:
    (repo / "README").write_text(f"{message}\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", message, "-q")
    return _git(repo, "rev-parse", "HEAD")
