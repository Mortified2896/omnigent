"""Regression tests for the bootstrap package handoff contract.

The 2026-08-10 incident shipped a handoff whose
``peer-deployer-package/bootstrap-manifest.json`` did NOT exist at
the outer canonical path the wrapper expected — the manifest was only
inside the tarball.  The wrapper refused to proceed and the operator
got a hard "missing manifest" failure with no persistent state
mutated.

These tests pin the contract so the failure mode can never recur:

  * the wrapper runs ``--verify-only`` against the EXACT final handoff
    directory and proves the preflight passes
  * the wrapper refuses (with a clear message) when the outer
    manifest is missing, missing at the canonical path, the tarball
    is missing, the SHA256SUMS is wrong, the inner manifest differs
    from the outer, or the tarball has unsafe entries
  * the path-resolution logic is independent of cwd or the operator's
    environment
  * the deterministic builder produces a layout the wrapper accepts
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "deploy" / "scripts" / "bootstrap-installer.sh"
BUILDER = REPO_ROOT / "deploy" / "scripts" / "build_control_room_peer_deployer_bootstrap.py"
BUILD_BMF = REPO_ROOT / "build_bootstrap_manifest.py"
SYS_PY = "/usr/bin/python3.11"


def _system_python() -> str:
    """Use the host python3.11 for builder runs (the bootstrap itself
    insists on 3.12.13, but the builder does not — it is plain stdlib).
    """
    return SYS_PY


def _resolve_build_id() -> str:
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return res.stdout.strip()


def _make_clean_source(work: Path) -> Path:
    """Create a small synthetic runtime source for package tests.

    The handoff builder only packages runtime files under ``deploy/`` plus a
    tiny allow-list of top-level metadata.  Copying the whole checkout (venvs,
    pytest temp trees, caches, prior handoffs) previously exploded temp usage;
    these tests need only the real runtime payload, not a multi-GB repo clone.
    """
    work.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "rsync",
            "-a",
            "--no-links",
            "--exclude=__pycache__/",
            "--exclude=*.pyc",
            "--exclude=.pytest_cache/",
            "--exclude=pytest-of-*/",
            "--exclude=crpd-btest-*/",
            "--exclude=venv/",
            "--exclude=.venv/",
            "--exclude=tmp/",
            f"{REPO_ROOT}/deploy/",
            f"{work}/deploy/",
        ],
        check=False, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    for name in ("build_bootstrap_manifest.py", "RELEASE_NOTES.md"):
        src = REPO_ROOT / name
        if src.is_file():
            shutil.copy2(src, work / name)
    # Minimal .git HEAD lets builder metadata derivation remain deterministic
    # when a test deliberately omits --build-id.
    (work / ".git").mkdir()
    (work / ".git" / "HEAD").write_text(_resolve_build_id() + "\n")
    return work


def _build_handoff(work: Path, *, build_id: str) -> Path:
    src = _make_clean_source(work / "src")
    out = work / "handoff"
    proc = subprocess.run(
        [
            _system_python(),
            str(BUILDER),
            "--source", str(src),
            "--build-id", build_id,
            "--output", str(out),
        ],
        check=False, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert out.is_dir()
    return out


def _run_verify_only(handoff: Path) -> subprocess.CompletedProcess:
    """Run the wrapper with --verify-only as the operator would.

    Always invokes the wrapper by absolute path with an explicit
    cwd that is unrelated to the handoff, to prove the wrapper
    resolves its own location.
    """
    return subprocess.run(
        ["bash", str(handoff / "bootstrap-installer.sh"), "--verify-only"],
        check=False, capture_output=True, text=True,
        cwd="/var/lib/omnigent-production/tmp",
    )


@pytest.fixture(scope="module")
def build_id() -> str:
    return _resolve_build_id()


@pytest.fixture(scope="module")
def handoff(tmp_path_factory: pytest.TempPathFactory, build_id: str) -> Path:
    work = tmp_path_factory.mktemp("peer-bootstrap-handoff")
    return _build_handoff(work, build_id=build_id)


# ---------------------------------------------------------------------------
# 1. expected package layout -> PASS
# ---------------------------------------------------------------------------


def test_expected_package_layout(handoff: Path) -> None:
    """The wrapper --verify-only passes on the canonical layout."""
    # Files exist and are NOT symlinks.
    for rel in (
        "bootstrap-installer.sh",
        "peer-deployer-package/peer-deployer-package.tar.gz",
        "peer-deployer-package/bootstrap-manifest.json",
        "peer-deployer-package/PACKAGE.json",
        "peer-deployer-package/SHA256SUMS",
    ):
        p = handoff / rel
        assert p.is_file(), f"missing or not a file: {p}"
        assert not p.is_symlink(), f"is a symlink: {p}"
    proc = _run_verify_only(handoff)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "OK -- verify-only" in proc.stdout


# ---------------------------------------------------------------------------
# 2. missing outer manifest -> FAIL before mutation
# ---------------------------------------------------------------------------


def test_missing_outer_manifest_fails_before_mutation(
    tmp_path: Path, handoff: Path
) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "bootstrap-installer.sh").write_bytes(
        (handoff / "bootstrap-installer.sh").read_bytes()
    )
    (bad / "peer-deployer-package").mkdir()
    for rel in ("peer-deployer-package.tar.gz",):
        (bad / "peer-deployer-package" / rel).write_bytes(
            (handoff / "peer-deployer-package" / rel).read_bytes()
        )
    # intentionally NO manifest, NO PACKAGE.json, NO SHA256SUMS
    proc = _run_verify_only(bad)
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert "missing required package file" in proc.stderr
    # No persistent state was created.
    assert not (bad / "opt").exists()


# ---------------------------------------------------------------------------
# 3. missing tarball -> FAIL before mutation
# ---------------------------------------------------------------------------


def test_missing_tarball_fails_before_mutation(tmp_path: Path, handoff: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "bootstrap-installer.sh").write_bytes(
        (handoff / "bootstrap-installer.sh").read_bytes()
    )
    (bad / "peer-deployer-package").mkdir()
    for rel in ("bootstrap-manifest.json", "PACKAGE.json", "SHA256SUMS"):
        (bad / "peer-deployer-package" / rel).write_bytes(
            (handoff / "peer-deployer-package" / rel).read_bytes()
        )
    # intentionally NO tarball
    proc = _run_verify_only(bad)
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert "missing required package file" in proc.stderr
    assert "peer-deployer-package.tar.gz" in proc.stderr


# ---------------------------------------------------------------------------
# 4. manifest at wrong/nested path -> FAIL clearly
# ---------------------------------------------------------------------------


def test_manifest_at_nested_path_fails(tmp_path: Path, handoff: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "bootstrap-installer.sh").write_bytes(
        (handoff / "bootstrap-installer.sh").read_bytes()
    )
    (bad / "peer-deployer-package").mkdir()
    (bad / "peer-deployer-package" / "nested").mkdir()
    # Place manifest under a sibling directory, not at the canonical path.
    (bad / "peer-deployer-package" / "nested" / "bootstrap-manifest.json").write_bytes(
        (handoff / "peer-deployer-package" / "bootstrap-manifest.json").read_bytes()
    )
    (bad / "peer-deployer-package" / "peer-deployer-package.tar.gz").write_bytes(
        (handoff / "peer-deployer-package" / "peer-deployer-package.tar.gz").read_bytes()
    )
    (bad / "peer-deployer-package" / "PACKAGE.json").write_bytes(
        (handoff / "peer-deployer-package" / "PACKAGE.json").read_bytes()
    )
    (bad / "peer-deployer-package" / "SHA256SUMS").write_bytes(
        (handoff / "peer-deployer-package" / "SHA256SUMS").read_bytes()
    )
    proc = _run_verify_only(bad)
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert "missing required package file" in proc.stderr
    assert "bootstrap-manifest.json" in proc.stderr


# ---------------------------------------------------------------------------
# 5. wrapper invoked from unrelated cwd -> still resolves correctly
# ---------------------------------------------------------------------------


def test_wrapper_invocation_from_unrelated_cwd(handoff: Path) -> None:
    proc = subprocess.run(
        ["bash", str(handoff / "bootstrap-installer.sh"), "--verify-only"],
        check=False, capture_output=True, text=True,
        cwd="/var/lib/omnigent-production/tmp",
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "OK -- verify-only" in proc.stdout


# ---------------------------------------------------------------------------
# 6. wrapper invoked through absolute path -> PASS
# ---------------------------------------------------------------------------


def test_wrapper_invocation_absolute_path(handoff: Path) -> None:
    proc = subprocess.run(
        ["bash", str(handoff / "bootstrap-installer.sh"), "--verify-only"],
        check=False, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "OK -- verify-only" in proc.stdout


# ---------------------------------------------------------------------------
# 7. tarball + manifest disagreement -> FAIL
# ---------------------------------------------------------------------------


def test_tarball_manifest_disagreement_fails(tmp_path: Path, handoff: Path) -> None:
    bad = tmp_path / "bad"
    shutil.copytree(handoff, bad)
    # Replace the outer manifest with a different file but recompute
    # SHA256SUMS so SHA256SUMS itself passes — the wrapper must still
    # catch the inner/outer manifest mismatch.
    fake = {
        "schema": "control-room-peer-deployer.bootstrap-manifest.v2",
        "build_id": _resolve_build_id(),
        "source_root": "omnigent",
        "file_count": 0,
        "hashes": {},
    }
    (bad / "peer-deployer-package" / "bootstrap-manifest.json").write_text(
        json.dumps(fake, indent=2, sort_keys=True)
    )
    import hashlib
    new_sha = hashlib.sha256(
        (bad / "peer-deployer-package" / "bootstrap-manifest.json").read_bytes()
    ).hexdigest()
    tar_sha = hashlib.sha256(
        (bad / "peer-deployer-package" / "peer-deployer-package.tar.gz").read_bytes()
    ).hexdigest()
    (bad / "peer-deployer-package" / "SHA256SUMS").write_text(
        f"{tar_sha}  peer-deployer-package.tar.gz\n{new_sha}  bootstrap-manifest.json\n"
    )
    proc = _run_verify_only(bad)
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert "manifest bytes differ" in proc.stderr


# ---------------------------------------------------------------------------
# 8. unexpected tar member / path traversal -> FAIL
# ---------------------------------------------------------------------------


def test_tarball_path_traversal_fails(tmp_path: Path, handoff: Path) -> None:
    bad = tmp_path / "bad"
    shutil.copytree(handoff, bad)
    # Inject an absolute path-like entry into the tarball and recompute
    # SHA256SUMS so the wrapper's SHA gategoes through.
    import tarfile, io
    new_tar = bad / "peer-deployer-package" / "peer-deployer-package.tar.gz"
    # Replace the tarball with one that has an absolute path entry.
    bad2 = bad / "_evilrebuild"
    bad2.mkdir()
    with tarfile.open(new_tar, "w:gz") as tf:
        ti = tarfile.TarInfo(name="/etc/passwd")
        ti.size = 1
        ti.mode = 0o644
        ti.type = tarfile.REGTYPE
        tf.addfile(ti, io.BytesIO(b"x"))
    # Use the existing manifest + PACKAGE.json (since the layout is
    # unchanged); recompute SHA256SUMS so the SHA gate passes.
    import hashlib
    new_sha = hashlib.sha256(new_tar.read_bytes()).hexdigest()
    manifest = bad / "peer-deployer-package" / "bootstrap-manifest.json"
    man_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (bad / "peer-deployer-package" / "SHA256SUMS").write_text(
        f"{new_sha}  peer-deployer-package.tar.gz\n{man_sha}  bootstrap-manifest.json\n"
    )
    proc = _run_verify_only(bad)
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert (
        "absolute paths" in proc.stderr
        or "path traversal" in proc.stderr
    )


# ---------------------------------------------------------------------------
# 9. --verify-only performs zero persistent mutation
# ---------------------------------------------------------------------------


def test_verify_only_no_persistent_mutation(
    tmp_path: Path, handoff: Path
) -> None:
    """Running --verify-only must not mutate persistent installer state.

    A repair run may start from an already-created partial install under
    /opt/control-room-peer-deployer or /var/lib/control-room-peer-deployer.
    Verify-only must leave that state untouched; it must not require the
    host to be pristine.
    """
    persistent_paths = [
        Path("/opt/control-room-peer-deployer"),
        Path("/var/lib/control-room-peer-deployer"),
        Path("/run/control-room-peer-deployer"),
        Path("/etc/systemd/system/control-room-peer-deployer.service"),
    ]
    before = {
        str(path): (
            os.path.lexists(path),
            os.readlink(path) if path.is_symlink() else None,
            path.stat().st_mtime_ns if os.path.exists(path) else None,
        )
        for path in persistent_paths
    }

    proc = _run_verify_only(handoff)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)

    after = {
        str(path): (
            os.path.lexists(path),
            os.readlink(path) if path.is_symlink() else None,
            path.stat().st_mtime_ns if os.path.exists(path) else None,
        )
        for path in persistent_paths
    }
    assert after == before

# ---------------------------------------------------------------------------
# 10. exact packaged handoff produced by builder passes --verify-only
# ---------------------------------------------------------------------------


def test_builder_produced_handoff_passes_verify_only(
    tmp_path: Path, build_id: str
) -> None:
    work = tmp_path / "build"
    handoff = _build_handoff(work, build_id=build_id)
    proc = _run_verify_only(handoff)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "OK -- verify-only" in proc.stdout
    # The summary must include the canonical paths.
    for marker in (
        str(handoff),
        str(handoff / "peer-deployer-package" / "peer-deployer-package.tar.gz"),
        str(handoff / "peer-deployer-package" / "bootstrap-manifest.json"),
    ):
        assert marker in proc.stdout, marker


# ---------------------------------------------------------------------------
# 11. manifest build_id matches PACKAGE.json build_id
# ---------------------------------------------------------------------------


def test_manifest_build_id_matches_package(handoff: Path) -> None:
    blob = json.loads(
        (handoff / "peer-deployer-package" / "PACKAGE.json").read_text()
    )
    pkg_bid = blob["build_id"]
    manifest = json.loads(
        (handoff / "peer-deployer-package" / "bootstrap-manifest.json").read_text()
    )
    assert manifest["build_id"] == pkg_bid


# ---------------------------------------------------------------------------
# 12. builder refuses to overwrite a non-empty --output unless --force
# ---------------------------------------------------------------------------


def test_builder_refuses_overwrite_without_force(
    tmp_path: Path, build_id: str
) -> None:
    work = tmp_path / "build"
    work.mkdir()
    out = work / "handoff"
    out.mkdir()
    (out / "marker").write_text("exists")
    src = _make_clean_source(work / "src")
    proc = subprocess.run(
        [
            _system_python(),
            str(BUILDER),
            "--source", str(src),
            "--build-id", build_id,
            "--output", str(out),
        ],
        check=False, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "REFUSED" in proc.stderr
    # Original contents preserved.
    assert (out / "marker").read_text() == "exists"


# ---------------------------------------------------------------------------
# 13. wrapper refuses to run if invoked path is a symlink
# ---------------------------------------------------------------------------


def test_wrapper_refuses_symlink_invocation(tmp_path: Path, handoff: Path) -> None:
    link = tmp_path / "via-symlink"
    link.symlink_to(handoff / "bootstrap-installer.sh")
    proc = subprocess.run(
        ["bash", str(link), "--verify-only"],
        check=False, capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
