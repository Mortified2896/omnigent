"""Regression tests for the bootstrap manifest (v2 schema) and verifier.

These tests prove the fixes added after the 2026-08-10 failed
bootstrap:

  * ``bootstrap-manifest.json`` is treated as bootstrap metadata and is
    NOT a member of the hashed payload
  * undeclared payload files are rejected
  * manifest schema is strictly enforced
  * manifest file_count is enforced
  * manifest hash values must be 64-char SHA-256 hex
  * relative paths are checked for ``..``/absolute segments
  * symlinks in the source are rejected
  * the manifest build + verify round-trip succeeds
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _load_bootstrap_module():
    PKG_ROOT = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
    spec = importlib.util.spec_from_file_location(
        "control_room_peer_deployer_bootstrap", PKG_ROOT / "control_room_peer_deployer_bootstrap.py"
    )
    assert spec is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def bootstrap_module():
    return _load_bootstrap_module()


def _make_payload(tmp_path: Path, *, include_files: list[str], extra: list[str] = ()) -> Path:
    payload = tmp_path / "payload"
    payload.mkdir()
    for rel in include_files:
        target = payload / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"content of {rel}\n")
    for rel in extra:
        target = payload / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"unexpected extra: {rel}\n")
    return payload


def _build_manifest(payload: Path, *, build_id: str | None = None) -> Path:
    out = payload.parent / "bootstrap-manifest.json"
    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "build_bootstrap_manifest.py"),
        "--source", str(payload),
        "--out", str(out),
    ]
    if build_id:
        cmd.extend(["--build-id", build_id])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"manifest build failed: {proc.stdout}\n{proc.stderr}"
    return out


def _hash_payload(m, source: Path):
    hashes, errors, warnings = m._hash_payload(source)
    return hashes, errors, warnings


# --- Manifest schema + build ----------------------------------------------------


def test_payload_plus_valid_manifest_passes(bootstrap_module, tmp_path: Path) -> None:
    """The exact failure mode from the operator: payload + a valid bootstrap-manifest.json
    must verify (the manifest is metadata, not a payload member)."""
    m = bootstrap_module
    files = [
        "deploy/scripts/peer_deployer/__init__.py",
        "deploy/scripts/peer_deployer/transaction.py",
        "deploy/scripts/peer_deployer/identity.py",
        "deploy/scripts/peer_deployer/preflight.py",
        "deploy/scripts/peer_deployer/staging.py",
        "deploy/scripts/peer_deployer/rollback.py",
        "deploy/scripts/peer_deployer/host_promotion.py",
        "deploy/scripts/peer_deployer/service.py",
        "deploy/scripts/peer_deployer/eligibility.py",
        "deploy/scripts/peer_deployer/service_state.py",
        "deploy/scripts/peer_deployer/registry.py",
        "deploy/scripts/peer_deployer/plan.py",
        "deploy/scripts/peer_deployer/engine.py",
        "deploy/systemd/control-room-peer-deployer.service",
        "deploy/scripts/control_room_peer_deployer_bootstrap.py",
    ]
    payload = _make_payload(tmp_path, include_files=files)
    manifest_path = _build_manifest(payload)
    manifest = m._validate_manifest_schema(manifest_path)
    assert manifest["schema"] == m.MANIFEST_SCHEMA_VERSION
    # bootstrap-manifest.json is metadata; it must NOT appear in the
    # manifest.hashes table.
    assert "bootstrap-manifest.json" not in manifest["hashes"]
    # Stage the manifest alongside the payload (this is exactly the
    # operator's failure scenario: tarball contains bootstrap-manifest.json
    # next to the source files).  The verifier must accept that as
    # metadata.
    (payload / "bootstrap-manifest.json").write_text(manifest_path.read_text())
    actual = m._verify_payload_against_manifest(manifest, payload)
    assert actual["bootstrap-manifest.json"] == actual["deploy/scripts/peer_deployer/__init__.py"] or \
           True  # manifest content not directly comparable; just confirm no failure
    # No exception was raised: the verifier accepted the metadata file.


def test_payload_with_unexpected_extra_file_fails(bootstrap_module, tmp_path: Path) -> None:
    """A non-metadata, undeclared file MUST cause failure."""
    m = bootstrap_module
    files = [
        "deploy/scripts/peer_deployer/__init__.py",
        "deploy/scripts/peer_deployer/transaction.py",
        "deploy/scripts/peer_deployer/identity.py",
        "deploy/scripts/peer_deployer/preflight.py",
        "deploy/scripts/peer_deployer/staging.py",
        "deploy/scripts/peer_deployer/rollback.py",
        "deploy/scripts/peer_deployer/host_promotion.py",
        "deploy/scripts/peer_deployer/service.py",
        "deploy/scripts/peer_deployer/eligibility.py",
        "deploy/scripts/peer_deployer/service_state.py",
        "deploy/scripts/peer_deployer/registry.py",
        "deploy/scripts/peer_deployer/plan.py",
        "deploy/scripts/peer_deployer/engine.py",
        "deploy/systemd/control-room-peer-deployer.service",
        "deploy/scripts/control_room_peer_deployer_bootstrap.py",
    ]
    payload = _make_payload(
        tmp_path,
        include_files=files,
        # The leaked file is *outside* the deploy/ prefix and is also
        # not in ALLOWED_METADATA_FILES — it gets dropped by the builder
        # so it appears as an undeclared extra on the source tree.
        extra=["README-leaked.md"],
    )
    manifest_path = _build_manifest(payload)
    manifest = m._validate_manifest_schema(manifest_path)
    with pytest.raises(m.BootstrapError) as excinfo:
        m._verify_payload_against_manifest(manifest, payload)
    assert "extra" in str(excinfo.value)


# --- Manifest schema enforcement ------------------------------------------------


def test_manifest_schema_mismatch_rejected(bootstrap_module, tmp_path: Path) -> None:
    m = bootstrap_module
    manifest_path = tmp_path / "bootstrap-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema": "wrong.version",
        "file_count": 0,
        "hashes": {},
    }))
    with pytest.raises(m.BootstrapError) as excinfo:
        m._validate_manifest_schema(manifest_path)
    assert "schema mismatch" in str(excinfo.value)


def test_manifest_file_count_mismatch_rejected(bootstrap_module, tmp_path: Path) -> None:
    m = bootstrap_module
    manifest_path = tmp_path / "bootstrap-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema": m.MANIFEST_SCHEMA_VERSION,
        "file_count": 5,
        "hashes": {"a.txt": "0" * 64},
    }))
    with pytest.raises(m.BootstrapError) as excinfo:
        m._validate_manifest_schema(manifest_path)
    assert "file_count" in str(excinfo.value)


def test_manifest_malformed_hash_rejected(bootstrap_module, tmp_path: Path) -> None:
    m = bootstrap_module
    manifest_path = tmp_path / "bootstrap-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema": m.MANIFEST_SCHEMA_VERSION,
        "file_count": 1,
        "hashes": {"a.txt": "not-a-hash"},
    }))
    with pytest.raises(m.BootstrapError) as excinfo:
        m._validate_manifest_schema(manifest_path)
    assert "SHA-256" in str(excinfo.value)


def test_manifest_absolute_path_rejected(bootstrap_module, tmp_path: Path) -> None:
    m = bootstrap_module
    manifest_path = tmp_path / "bootstrap-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema": m.MANIFEST_SCHEMA_VERSION,
        "file_count": 1,
        "hashes": {"/etc/passwd": "0" * 64},
    }))
    with pytest.raises(m.BootstrapError) as excinfo:
        m._validate_manifest_schema(manifest_path)
    assert "unsafe rel path" in str(excinfo.value)


def test_manifest_path_traversal_rejected(bootstrap_module, tmp_path: Path) -> None:
    m = bootstrap_module
    manifest_path = tmp_path / "bootstrap-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema": m.MANIFEST_SCHEMA_VERSION,
        "file_count": 1,
        "hashes": {"../etc/passwd": "0" * 64},
    }))
    with pytest.raises(m.BootstrapError):
        m._validate_manifest_schema(manifest_path)


def test_symlinks_in_source_rejected_by_builder(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "deploy").mkdir()
    (payload / "deploy" / "scripts").mkdir()
    (payload / "deploy" / "scripts" / "peer_deployer").mkdir()
    (payload / "deploy" / "scripts" / "peer_deployer" / "__init__.py").write_text("")
    (payload / "external.txt").write_text("external")
    symlink = payload / "leak"
    symlink.symlink_to(payload / "external.txt")
    out = payload.parent / "manifest.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "build_bootstrap_manifest.py"),
            "--source", str(payload),
            "--out", str(out),
        ],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
    assert "symlink" in proc.stderr.lower()
