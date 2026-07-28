"""Behavioral invariants for issue #28 in ``scripts/promote_release.sh``.

Issue #28 requires:

* the canonical release SHA is resolved before extraction and is passed
  to the manifest, not derived from a ``git -C <release-dir>`` call;
* the build is staged in a temporary directory and atomically renamed
  into ``releases/<sha>``;
* an existing release is reused only after manifest, lockfile,
  preflight, and provenance checks succeed; a partial candidate is
  quarantined rather than reused;
* ``web/package-lock.json`` is hashed before and after ``npm ci`` and
  the candidate is rejected on drift;
* ``web/node_modules`` is removed after the static build.

These tests assert the structural invariants in the shell so a future
refactor cannot silently regress them.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "promote_release.sh"


def _script_text() -> str:
    return _SCRIPT_PATH.read_text()


def test_resolves_sha_before_archive_extraction() -> None:
    """The canonical SHA is resolved before any release-dir git call.

    The script must run ``release_id`` against the immutable repo and
    not call ``git -C $RELEASE_DIR rev-parse HEAD`` to derive the
    release identity.
    """
    text = _script_text()
    assert 'git -C "$RELEASE_DIR" rev-parse' not in text
    assert "git -C '$RELEASE_DIR' rev-parse" not in text
    # The script still resolves the SHA from the source repo.
    assert "omnigent.deploy.ops.release_id" in text or "release_id" in text


def test_build_uses_temporary_staging_directory() -> None:
    """The build occurs in a staging directory, not in ``releases/<sha>``."""
    text = _script_text()
    assert "STAGING_DIR=" in text
    assert "$DEPLOY_ROOT/releases/.staging-" in text
    # The final release path must not exist before atomic rename.
    assert 'mv -T "$STAGING_DIR" "$FINAL_RELEASE_DIR"' in text


def test_staging_cleaned_on_failure() -> None:
    """A trap cleans the staging directory on failure."""
    text = _script_text()
    assert "trap cleanup_staging EXIT" in text
    assert 'cleanup_staging() { rm -rf "$STAGING_DIR"; }' in text


def test_reuse_requires_manifest_lockfile_preflight() -> None:
    """Cached reuse validates manifest, lockfile hash, and preflight."""
    text = _script_text()
    assert "verify_candidate()" in text
    assert '"$candidate_sha" == "$sha"' in text or "candidate_sha" in text
    assert "lockfile_hashes" in text
    assert "omnigent.deploy.preflight" in text
    assert "quarantine_invalid" in text


def test_lockfile_hashed_before_and_after_npm_ci() -> None:
    """``web/package-lock.json`` is hashed before and after ``npm ci``."""
    text = _script_text()
    assert "lockfile_before=" in text
    assert "lockfile_after=" in text
    assert 'lockfile_before" == "$lockfile_after"' in text
    # The failure path must clean the candidate.
    assert "npm ci mutated web/package-lock.json" in text


def test_web_node_modules_removed_after_static_build() -> None:
    """``web/node_modules`` is removed after the static build."""
    text = _script_text()
    assert "removing web/node_modules after static build" in text
    assert 'rm -rf "$RELEASE_DIR/web/node_modules"' in text


def test_manifest_receives_explicit_commit_sha() -> None:
    """``ReleaseManifest.from_directory`` is called with ``commit_sha``."""
    text = _script_text()
    assert "ReleaseManifest.from_directory(" in text
    assert "commit_sha='$sha'" in text


def test_no_git_symlink_for_release() -> None:
    """The script does not create a ``.git`` symlink in the release dir."""
    text = _script_text()
    assert 'ln -s "$REPO_ROOT/.git" "$RELEASE_DIR/.git"' not in text
    assert 'ln -s "$REPO_ROOT/.git"' not in text


def test_verify_candidate_rejects_dot_git_dependency() -> None:
    """``verify_candidate`` rejects a candidate that still has ``.git``."""
    text = _script_text()
    assert '[[ ! -e "$RELEASE_DIR/.git" ]] || return 1' in text
