#!/usr/bin/env python3
"""Build a SHA-256 manifest of the immutable bootstrap payload.

Run this from the source checkout once a release is final.  The
bootstrap installer will refuse to install anything whose hashes do
not match this manifest.  Do NOT regenerate the manifest after the
operator has run ``sudo bootstrap.py`` — that would break supply-chain
provenance.

Usage:

    python build_bootstrap_manifest.py \\
        --source /path/to/checked-out-source \\
        --out /path/to/checked-out-source/bootstrap-manifest.json

The manifest is treated as bootstrap metadata by the installer (it
is the file that lists the hashes, and is not its own member).  The
installer only allows the manifest as a recognised metadata file;
any other extra payload file causes a hard verification failure.

Manifest schema (v2):

  * ``schema``: literal
    ``"control-room-peer-deployer.bootstrap-manifest.v2"``
  * ``build_id``: 40-char Git commit SHA (set via ``--build-id``)
  * ``source_root``: relative path string (no absolute paths) or null
  * ``file_count``: integer equal to len(hashes)
  * ``hashes``: dict[relpath -> 64-char SHA-256]
    - ``relpath`` is forward-slash separated, never ``.`` or ``..``
    - ``relpath`` is never absolute
    - hashes are 64-char lower-hex SHA-256

By default only the per-runtime files are hashed
(``deploy/`` plus the focused bootstrap helpers plus a few required
top-level files).  Pass ``--full`` to hash the entire tree
(debugging only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MANIFEST_SCHEMA = "control-room-peer-deployer.bootstrap-manifest.v2"
RUNTIME_PREFIXES = ("deploy/",)
REQUIRED_PAYLOAD_FILES = (
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
)
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _is_runtime(rel: str) -> bool:
    if rel.startswith("deploy/"):
        return True
    if rel == "build_bootstrap_manifest.py":
        return True
    if rel == "RELEASE_NOTES.md":
        return True
    return False


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_relpath(rel: str) -> bool:
    if not rel or "\\" in rel:
        return False
    parts = rel.split("/")
    if any(part in ("", ".") for part in parts):
        return False
    if any(part == ".." for part in parts):
        return False
    if os.path.isabs(rel):
        return False
    return True


def _derive_build_id(source: Path) -> str:
    """Try to derive a Git commit SHA from ``source``.

    Fall back to ``UNKNOWN`` if there is no Git checkout.  A non-
    canonical build_id is still permitted by the schema (the build_id
    is purely informational), but a valid SHA when present is always
    preferred.
    """
    git_dir = source / ".git"
    if not git_dir.exists():
        return "0" * 40
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return "0" * 40
    if head.startswith("ref:"):
        ref = head.split(" ", 1)[1]
        ref_path = git_dir / ref
        if ref_path.exists():
            try:
                return ref_path.read_text().strip()
            except OSError:
                pass
    elif SHA_RE.fullmatch(head):
        return head
    return "0" * 40


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--full", action="store_true",
                    help="hash every file (debugging only)")
    ap.add_argument("--build-id", type=str, default=None,
                    help="explicit 40-char build_id; otherwise derived from HEAD")
    ns = ap.parse_args()
    if not ns.source.is_dir():
        sys.stderr.write(f"missing source: {ns.source}\n"); return 2

    files: dict[str, str] = {}
    for p in sorted(ns.source.rglob("*")):
        if p.is_symlink():
            # Symlinks are rejected by the installer; refuse to hash them.
            sys.stderr.write(
                f"REFUSED: source contains symlink: {p} (payload must not contain symlinks)\n"
            )
            return 2
        if not p.is_file():
            continue
        rel = p.relative_to(ns.source).as_posix()
        if rel.endswith("__pycache__"):
            continue
        if rel == "bootstrap-manifest.json":
            # Manifest is bootstrap metadata; it MUST NOT hash itself.
            continue
        if "/.git/" in ("/" + rel):
            continue
        if rel.endswith((".pyc", ".wasm", ".map")):
            continue
        if not _safe_relpath(rel):
            sys.stderr.write(f"REFUSED: unsafe payload path: {rel!r}\n")
            return 2
        if not ns.full and not _is_runtime(rel):
            continue
        h = _hash_file(p)
        if not SHA256_RE.fullmatch(h):
            sys.stderr.write(f"REFUSED: malformed hash: {rel}\n"); return 2
        files[rel] = h

    for required in REQUIRED_PAYLOAD_FILES:
        if required not in files:
            sys.stderr.write(
                f"REFUSED: required payload file missing in source: {required}\n"
            )
            return 2

    if ns.build_id is not None:
        if not SHA_RE.fullmatch(ns.build_id):
            sys.stderr.write(
                f"--build-id must be a 40-char SHA: {ns.build_id!r}\n"
            )
            return 2
        build_id = ns.build_id
    else:
        build_id = _derive_build_id(ns.source)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "build_id": build_id,
        "source_root": ns.source.name,
        "file_count": len(files),
        "hashes": dict(sorted(files.items())),
    }
    ns.out.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(
        f"wrote manifest with {len(files)} entries to {ns.out} "
        f"(schema={MANIFEST_SCHEMA}, build_id={build_id[:12]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
