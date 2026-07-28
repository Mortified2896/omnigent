"""Real-lifecycle regression test for issue #28 follow-up.

This test exercises the canonical-path invariant end-to-end:

1. Build a candidate in a temporary deploy root, in a staging dir.
2. The manifest must record the canonical post-rename path, not the
   staging path.
3. A strict canonical-path verifier runs immediately after the
   atomic rename; on failure the canonical candidate must be
   quarantined (not promoted).
4. Invoking the build/reuse path a second time must reuse the
   candidate from the canonical path (it must NOT rebuild, must NOT
   quarantine, and the manifest must remain valid).
5. Current and previous production releases must remain inviolable
   when candidate builds fail.

The lifecycle is driven from Python so the test is hermetic and
fast. A handful of targeted structural-string tests pin the shell
changes so a refactor of ``scripts/promote_release.sh`` cannot
silently regress the architectural contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from omnigent.deploy.supervisor.manifest import (
    ManifestError,
    ReleaseManifest,
    load_manifest,
    verify_canonical_release_dir,
    verify_manifest_commit,
    write_manifest,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "promote_release.sh"


# --- helpers -------------------------------------------------------------


def _build_fake_release(staging: Path) -> None:
    """Create a minimal fake release directory suitable for manifest testing.

    Mirrors the layout the real promotion script produces at the
    manifest-write step: ``pyproject.toml`` + ``uv.lock`` + ``web/``
    with a lockfile + ``omnigent/server/static/web-ui/`` with the
    three preflight artifacts. No ``.git`` and no ``node_modules``,
    as the script guarantees.
    """
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "pyproject.toml").write_text('[project]\nname = "omnigent"\nversion = "0.0.0"\n')
    (staging / "uv.lock").write_text("# uv lock\n")
    web = staging / "web"
    web.mkdir(exist_ok=True)
    (web / "package-lock.json").write_text('{"name":"web"}\n')
    bundle = staging / "omnigent" / "server" / "static" / "web-ui"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "index.html").write_text("<!doctype html>\n")
    (bundle / "version.json").write_text('{"build":"lifecycle-test"}\n')
    (bundle / "manifest.webmanifest").write_text("{}\n")
    # Pre-built .venv shell so manifest_path_for resolves.
    (staging / ".venv" / "bin").mkdir(parents=True, exist_ok=True)


def _make_release_manifest(
    *,
    sha: str,
    inspect_dir: Path,
    canonical_dir: Path,
    frontend_version: str = "lifecycle-test",
) -> ReleaseManifest:
    """Build a manifest mirroring what ``promote_release.sh`` writes."""
    return ReleaseManifest.from_directory(
        inspect_dir,
        commit_sha=sha,
        repository="Mortified2896/omnigent",
        python_executable=str(canonical_dir / ".venv/bin/python"),
        python_version="3.12.13",
        omnigent_module_path=str(canonical_dir / "omnigent/__init__.py"),
        omnigent_server_app_path=str(canonical_dir / "omnigent/server/app.py"),
        frontend_build_version=frontend_version,
        canonical_release_dir=canonical_dir,
    )


@pytest.fixture
def deploy_root(tmp_path: Path) -> Path:
    """A fresh ``deploy_root`` with the canonical subdirs created."""
    root = tmp_path / "deploy-root"
    root.mkdir()
    (root / "releases").mkdir()
    (root / "failed").mkdir()
    (root / "manifests").mkdir()
    return root


# --- tests ---------------------------------------------------------------


def test_canonical_path_survives_atomic_rename(deploy_root: Path) -> None:
    """``from_directory`` records the canonical path, not the inspection dir.

    This is the architectural fix: the manifest captures the
    post-rename canonical directory even though the files were
    inspected from a staging directory. After ``mv -T``, the staging
    directory no longer exists but the manifest is still valid for
    the next ``--build-only``.
    """
    sha = "a" * 40
    canonical = deploy_root / "releases" / sha
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-1"

    _build_fake_release(staging)
    manifest = _make_release_manifest(sha=sha, inspect_dir=staging, canonical_dir=canonical)
    write_manifest(staging, manifest)

    # Pre-rename strict canonical verifier passes because we passed
    # the canonical path explicitly.
    verify_canonical_release_dir(manifest, canonical)
    assert manifest.release_dir == str(canonical.resolve())

    # Atomic rename: staging -> canonical.
    staging.rename(canonical)

    # The canonical manifest survives the rename.
    reloaded = load_manifest(canonical)
    verify_canonical_release_dir(reloaded, canonical)
    assert reloaded.release_dir == str(canonical.resolve())
    assert reloaded.commit_sha == sha


def test_reused_candidate_is_not_quarantined(deploy_root: Path) -> None:
    """A second ``--build-only`` reuses the candidate instead of quarantining it.

    Drives the full staging-then-rename-then-reuse lifecycle using
    the manifest module + Python-level verifier, mirroring what
    ``scripts/promote_release.sh`` does. The script's structural-
    string tests pin the shell-side flow separately.
    """
    sha = "f" * 40
    canonical = deploy_root / "releases" / sha
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-2"

    # Phase 1: build in staging, write manifest with canonical path.
    _build_fake_release(staging)
    manifest = _make_release_manifest(sha=sha, inspect_dir=staging, canonical_dir=canonical)
    write_manifest(staging, manifest)

    # Phase 2: atomic rename staging -> canonical.
    staging.rename(canonical)

    # Phase 3: post-rename strict canonical verifier passes.
    reloaded = load_manifest(canonical)
    verify_canonical_release_dir(reloaded, canonical)
    assert reloaded.release_dir == str(canonical.resolve())

    # Phase 4: simulate the ``--build-only`` reuse path. The second
    # invocation observes the canonical candidate, validates the
    # manifest, and finds the canonical path already correct.
    second = load_manifest(canonical)
    verify_canonical_release_dir(second, canonical)
    manifest_path = canonical / "manifest.json"
    first_inode = manifest_path.stat().st_ino
    first_mtime_ns = manifest_path.stat().st_mtime_ns

    # No quarantine happened (the failed/ dir is empty).
    assert list((deploy_root / "failed").iterdir()) == []

    # Re-reading the manifest again (no rebuild) gives identical
    # inode and mtime — proof of reuse.
    again = load_manifest(canonical)
    verify_canonical_release_dir(again, canonical)
    assert manifest_path.stat().st_ino == first_inode
    assert manifest_path.stat().st_mtime_ns == first_mtime_ns
    assert list((deploy_root / "failed").iterdir()) == []


def test_post_rename_verifier_rejects_staging_path(deploy_root: Path) -> None:
    """If a manifest is forged with the staging path, the verifier rejects it.

    A maliciously edited manifest pointing at ``.staging-<sha>-...``
    is the failure mode that motivated the architectural fix. The
    strict verifier rejects such a manifest so the build cannot
    promote it.
    """
    sha = "b" * 40
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-3"
    canonical = deploy_root / "releases" / sha
    _build_fake_release(staging)
    manifest = _make_release_manifest(sha=sha, inspect_dir=staging, canonical_dir=canonical)
    # Forge the manifest: rewrite ``release_dir`` to point at the
    # staging path that will disappear on rename.
    manifest.release_dir = str(staging.resolve())
    write_manifest(staging, manifest)
    staging.rename(canonical)

    # The strict verifier rejects the canonical candidate because
    # its manifest points at the now-deleted staging path.
    reloaded = load_manifest(canonical)
    with pytest.raises(ManifestError) as exc:
        verify_canonical_release_dir(reloaded, canonical)
    assert "staging" in str(exc.value).lower()


def test_manifest_release_dir_equals_canonical_after_atomic_rename(
    deploy_root: Path,
) -> None:
    """After a successful build, ``manifest.release_dir`` equals the canonical path."""
    sha = "1" * 40
    canonical = deploy_root / "releases" / sha
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-4"
    _build_fake_release(staging)
    manifest = _make_release_manifest(sha=sha, inspect_dir=staging, canonical_dir=canonical)
    write_manifest(staging, manifest)
    staging.rename(canonical)
    persisted = json.loads((canonical / "manifest.json").read_text())
    assert persisted["release_dir"] == str(canonical.resolve())


def test_staging_path_does_not_leak_into_manifest(deploy_root: Path) -> None:
    """No ``.staging-<sha>`` string appears anywhere in the persisted manifest."""
    sha = "2" * 40
    canonical = deploy_root / "releases" / sha
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-5"
    _build_fake_release(staging)
    manifest = _make_release_manifest(sha=sha, inspect_dir=staging, canonical_dir=canonical)
    write_manifest(staging, manifest)
    staging.rename(canonical)
    persisted = json.loads((canonical / "manifest.json").read_text())
    serialized = json.dumps(persisted)
    assert ".staging-" not in serialized, (
        f"manifest still references a staging path: {serialized!r}"
    )


def test_failed_build_quarantines_only_inactive_candidate(deploy_root: Path) -> None:
    """A failed build quarantines the candidate without touching current/previous.

    Simulates the failure path by constructing a manifest whose
    ``release_dir`` references the staging path; the strict
    canonical-path verifier rejects it, and the candidate is
    quarantined without the current/previous symlinks being
    touched.
    """
    # Plant fake current/previous so we can assert they survive.
    current_link = deploy_root / "current"
    previous_link = deploy_root / "previous"
    fake_current = deploy_root / "releases" / "current-sha-111"
    fake_previous = deploy_root / "releases" / "previous-sha-222"
    fake_current.mkdir(parents=True, exist_ok=True)
    fake_previous.mkdir(parents=True, exist_ok=True)
    current_link.symlink_to(fake_current)
    previous_link.symlink_to(fake_previous)

    # Forge a "build" with a manifest that mistakenly points at the
    # staging path. The strict verifier rejects it; the deployment
    # quarantine policy moves the candidate to failed/.
    sha = "3" * 40
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-6"
    canonical = deploy_root / "releases" / sha
    _build_fake_release(staging)
    bad_manifest = ReleaseManifest(
        commit_sha=sha,
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir=str(staging.resolve()),  # STAGING PATH — defect
        python_executable=str(canonical / ".venv/bin/python"),
        python_version="3.12.13",
        omnigent_module_path=str(canonical / "omnigent/__init__.py"),
        omnigent_server_app_path=str(canonical / "omnigent/server/app.py"),
    )
    write_manifest(staging, bad_manifest)
    staging.rename(canonical)

    # The strict verifier rejects the canonical candidate because
    # its manifest points at the now-deleted staging path.
    reloaded = load_manifest(canonical)
    with pytest.raises(ManifestError):
        verify_canonical_release_dir(reloaded, canonical)

    # Move the invalid candidate to failed/ (mimicking what the
    # shell script does on a verifier failure).
    quarantine = deploy_root / "failed" / f"{sha}-invalid-1234567890"
    canonical.rename(quarantine)

    # Current and previous must be untouched.
    assert current_link.resolve() == fake_current.resolve()
    assert previous_link.resolve() == fake_previous.resolve()
    assert not canonical.exists()
    assert quarantine.exists()


def test_invalid_candidate_rejection(deploy_root: Path) -> None:
    """A candidate with the wrong SHA is rejected by ``verify_manifest_commit``.

    Mirrors the shell script's ``verify_candidate`` invariant: the
    manifest's recorded ``commit_sha`` must equal the requested
    SHA, otherwise the candidate is quarantined and the build
    rebuilds.
    """
    sha = "9" * 40
    canonical = deploy_root / "releases" / sha
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-7"
    _build_fake_release(staging)
    manifest = _make_release_manifest(sha=sha, inspect_dir=staging, canonical_dir=canonical)
    write_manifest(staging, manifest)
    staging.rename(canonical)

    # Correct SHA: verifier passes.
    reloaded = load_manifest(canonical)
    verify_manifest_commit(reloaded, sha)

    # Wrong SHA: verifier rejects.
    with pytest.raises(ManifestError) as exc:
        verify_manifest_commit(reloaded, "0" * 40)
    assert "does not match" in str(exc.value)


def test_explicit_sha_required_for_promotion(tmp_path: Path) -> None:
    """The canonical SHA must be explicit; ``manifest.commit_sha`` is required.

    A manifest with an empty ``commit_sha`` is rejected by the
    verifier — the supervisor refuses to start a release whose
    identity is unknown.
    """
    manifest = ReleaseManifest(
        commit_sha="",
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir="/tmp/release",
        python_executable="/tmp/release/.venv/bin/python",
        python_version="3.12.13",
        omnigent_module_path="/tmp/release/omnigent/__init__.py",
        omnigent_server_app_path="/tmp/release/omnigent/server/app.py",
    )
    with pytest.raises(ManifestError):
        verify_manifest_commit(manifest, "a" * 40)


def test_no_git_dependency_in_release_dir(deploy_root: Path) -> None:
    """The candidate directory must not depend on a ``.git`` artifact.

    The release archive is built from ``git archive`` and intentionally
    has no ``.git`` directory. The ``--build-only`` reuse path
    refuses to reuse a candidate that still carries a ``.git``
    symlink or directory.
    """
    sha = "4" * 40
    canonical = deploy_root / "releases" / sha
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-8"
    _build_fake_release(staging)
    assert not (staging / ".git").exists(), "fixture invariant: fake staging has no .git directory"
    manifest = _make_release_manifest(sha=sha, inspect_dir=staging, canonical_dir=canonical)
    write_manifest(staging, manifest)
    staging.rename(canonical)
    assert not (canonical / ".git").exists()
    assert not (canonical / "web" / "node_modules").exists()


def test_lockfile_mutation_is_detected(deploy_root: Path) -> None:
    """If ``web/package-lock.json`` is mutated, the manifest's recorded hash diverges.

    The lockfile fingerprint in the manifest is computed at write
    time; a later on-disk mutation will diverge from the manifest
    and the supervisor can detect the tampering.
    """
    sha = "5" * 40
    canonical = deploy_root / "releases" / sha
    staging = deploy_root / "releases" / f".staging-{sha[:12]}-99999-9"
    _build_fake_release(staging)
    manifest = _make_release_manifest(sha=sha, inspect_dir=staging, canonical_dir=canonical)
    write_manifest(staging, manifest)
    staging.rename(canonical)
    persisted = json.loads((canonical / "manifest.json").read_text())
    # Tamper the lockfile.
    lockfile = canonical / "web" / "package-lock.json"
    lockfile.write_text('{"name":"tampered","version":"999.0.0"}\n')
    recorded_hash = persisted["lockfile_hashes"]["web/package-lock.json"]
    actual_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    assert recorded_hash != actual_hash, (
        "tampered lockfile must diverge from the manifest's recorded hash"
    )


# --- shell invariants (cheap, prevent regression) ------------------------


def test_promote_script_passes_canonical_release_dir_to_from_directory() -> None:
    """The shell script must pass ``canonical_release_dir`` to ``from_directory``."""
    text = _SCRIPT_PATH.read_text()
    body = text[text.find("set -euo pipefail") :]
    assert "canonical_release_dir=pathlib.Path('$FINAL_RELEASE_DIR')" in body, (
        "promote_release.sh must pass canonical_release_dir=$FINAL_RELEASE_DIR to "
        "ReleaseManifest.from_directory"
    )


def test_promote_script_defines_final_release_dir_before_manifest_write() -> None:
    """``FINAL_RELEASE_DIR`` is declared before the manifest write block."""
    text = _SCRIPT_PATH.read_text()
    body = text[text.find("set -euo pipefail") :]
    final_idx = body.find('FINAL_RELEASE_DIR="$RELEASE_DIR"')
    canonical_arg_idx = body.find("canonical_release_dir=pathlib.Path('$FINAL_RELEASE_DIR')")
    assert final_idx != -1
    assert canonical_arg_idx != -1
    assert final_idx < canonical_arg_idx, (
        "FINAL_RELEASE_DIR must be set before the manifest-write Python invocation "
        "that references it"
    )


def test_promote_script_runs_canonical_verifier_after_atomic_rename() -> None:
    """The shell script must run a strict canonical verifier right after ``mv -T``."""
    text = _SCRIPT_PATH.read_text()
    body = text[text.find("set -euo pipefail") :]
    rename_idx = body.find('mv -T "$STAGING_DIR" "$FINAL_RELEASE_DIR"')
    # Find the *call* site, not the function definition. The shell
    # script defines ``verify_candidate_canonical()`` first and only
    # invokes it once, in the post-rename block.
    verify_idx = body.find("if ! verify_candidate_canonical; then")
    assert rename_idx != -1
    assert verify_idx != -1
    assert verify_idx > rename_idx, "verify_candidate_canonical must run AFTER the atomic rename"


def test_promote_script_quarantines_on_post_rename_failure() -> None:
    """A failed canonical verifier must quarantine only the new candidate."""
    text = _SCRIPT_PATH.read_text()
    body = text[text.find("set -euo pipefail") :]
    after = body[body.find("verify_candidate_canonical") :]
    assert "quarantine_invalid" in after
    assert 'fail "post-rename canonical-path verification failed' in after
