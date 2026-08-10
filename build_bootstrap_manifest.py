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

By default only files needed at runtime are hashed
(``deploy/`` plus the focused tests plus this builder).  Pass
``--full`` to hash the entire tree (debugging only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

RUNTIME_PREFIXES = (
    "deploy/",
)


def _is_runtime(rel: str, tests: bool, builder: bool) -> bool:
    if rel.startswith("deploy/"):
        return True
    if tests and rel.startswith("tests/deploy/test_peer_deployer_eligibility.py"):
        return True
    if tests and rel.startswith("tests/deploy/test_peer_deployer_service.py"):
        return True
    if builder and rel == "build_bootstrap_manifest.py":
        return True
    return False


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--full", action="store_true", help="hash every file (debugging)")
    ns = ap.parse_args()
    if not ns.source.is_dir():
        sys.stderr.write(f"missing source: {ns.source}\n"); return 2
    files = {}
    for p in sorted(ns.source.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ns.source).as_posix()
        if rel.endswith("__pycache__"):
            continue
        if "/.git/" in ("/" + rel):
            continue
        if rel.endswith((".pyc", ".wasm", ".map")):
            continue
        if rel == "bootstrap-manifest.json":
            continue
        if not ns.full and not _is_runtime(rel, tests=True, builder=True):
            continue
        files[rel] = _hash_file(p)
    manifest = {
        "schema": "control-room-peer-deployer.bootstrap-manifest.v1",
        "source_root": str(ns.source),
        "file_count": len(files),
        "hashes": files,
    }
    ns.out.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"wrote manifest with {len(files)} entries to {ns.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())