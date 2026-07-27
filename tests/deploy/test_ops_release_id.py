"""Tests for the SHA-resolution helpers used by ``scripts/promote_release.sh``."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.deploy.ops import release_id


def test_normalize_ref_passes_through_full_sha(tmp_path: Path) -> None:
    """A 40-character SHA is returned unchanged."""
    full = "0123456789abcdef0123456789abcdef01234567"
    assert release_id.normalize_ref(tmp_path, full) == full


def test_normalize_ref_rejects_invalid_chars(tmp_path: Path) -> None:
    """A ref-like string with disallowed characters is rejected."""
    with pytest.raises(release_id.ReleaseIdError) as exc:
        release_id.normalize_ref(tmp_path, "evil;rm -rf /")
    assert "not a valid" in str(exc.value)


def test_normalize_ref_short_sha_expands(tmp_path: Path) -> None:
    """Short SHAs are expanded via ``git rev-parse``."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"],
        check=True,
    )
    (repo / "a").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
    short = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    expected_full = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert release_id.normalize_ref(repo, short) == expected_full


def test_normalize_ref_branch(tmp_path: Path) -> None:
    """Branch names resolve to the underlying SHA."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"],
        check=True,
    )
    (repo / "a").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "branch", "release/2026-Q3"],
        check=True,
    )
    short = release_id.normalize_ref(repo, "release/2026-Q3")
    assert len(short) == 40


def test_normalize_ref_missing_repo(tmp_path: Path) -> None:
    """Missing repo path raises."""
    with pytest.raises(release_id.ReleaseIdError):
        release_id.normalize_ref(tmp_path, "HEAD")


def test_normalize_ref_empty_defaults_to_fork_main(tmp_path: Path) -> None:
    """An empty ref falls back to ``fork/main`` (the documented default)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"],
        check=True,
    )
    (repo / "a").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    # Set up a "fork" remote pointing at the same repo (we don't have a
    # network in CI; ``fetch`` is a no-op).
    subprocess.run(["git", "-C", str(repo), "remote", "add", "fork", str(repo)], check=True)
    full = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Empty ref must be rejected (we have no fork/main tracking).
    with pytest.raises(release_id.ReleaseIdError):
        release_id.normalize_ref(repo, "")
    # Now create a fork/main branch-like ref by fetching into fork/main.
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/fork/main", full], check=True)
    assert release_id.normalize_ref(repo, "") == full
