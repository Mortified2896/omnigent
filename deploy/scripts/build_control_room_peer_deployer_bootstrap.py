#!/usr/bin/env python3
"""Deterministic builder for the ONE-TIME peer-deployer bootstrap handoff.

This is the canonical, reproducible generator for the bootstrap
package directory that the operator hands to ``sudo bash
<dir>/bootstrap-installer.sh``.

Output layout (everything is REQUIRED and the wrapper will refuse to
install if any of these is missing, a symlink, or wrong-sized):

    <output-dir>/
        bootstrap-installer.sh                          (regular file)
        peer-deployer-package/
            peer-deployer-package.tar.gz                (regular file)
            bootstrap-manifest.json                     (regular file)
            PACKAGE.json                                (regular file)
            SHA256SUMS                                  (regular file)

Contract layers (explicit, not implicit):

  A. ``PACKAGE.json``     - immutable package metadata (build_id, git
                            commit, layout version, builder version,
                            generated_at, source root).  Verified by
                            the wrapper BEFORE invoking the bootstrap.
  B. ``SHA256SUMS``       - SHA-256 of the tarball AND the outer
                            ``bootstrap-manifest.json``.  Verified by
                            the wrapper BEFORE invoking the bootstrap.
  C. ``bootstrap-manifest.json`` (outer)  - schema v2 manifest of the
                            payload.  Verified by the wrapper to
                            resolve the canonical path the bootstrap
                            module expects.  The same content is also
                            embedded inside the tarball so the
                            payload-digest binding is intact.
  D. ``peer-deployer-package.tar.gz``     - the immutable source
                            payload.  Embeds ``bootstrap-manifest.json``
                            AND every file the inner verifier hashes.

The wrapper MUST agree with this exact layout.  If the wrapper and
the builder ever disagree, the wrapper is wrong, not the operator.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

# Bump when the layout contract changes.  Recorded in PACKAGE.json and
# enforced by the wrapper preflight.
PACKAGE_LAYOUT_VERSION = "control-room-peer-deployer.bootstrap-package.v1"
MANIFEST_SCHEMA = "control-room-peer-deployer.bootstrap-manifest.v2"

REQUIRED_OUTER_FILES = (
    "bootstrap-installer.sh",
    "peer-deployer-package/peer-deployer-package.tar.gz",
    "peer-deployer-package/bootstrap-manifest.json",
    "peer-deployer-package/PACKAGE.json",
    "peer-deployer-package/SHA256SUMS",
)

# Files we MUST NOT include in the source payload (output pollution,
# transient state, etc.).
EXCLUDED_BASENAMES = {
    "bootstrap-manifest.json",  # emitted deterministically below
    "PEERDEPLOYER_BUILD_TMP",   # builder scratch
}

# Repo paths that are projections of test data or fixtures (zero or
# near-zero runtime relevance to the peer-deployer payload).  We still
# include them only if they are explicit runtime files; the safe
# default is to mirror what the existing build_bootstrap_manifest.py
# considers "runtime".
RUNTIME_PREFIXES = ("deploy/",)
RUNTIME_EXTRA_BASENAMES = {
    "build_bootstrap_manifest.py",
    "RELEASE_NOTES.md",
}

SHA_RE = re.compile(r"[0-9a-f]{40}")


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_relpath(rel: str) -> bool:
    if not rel or "\\" in rel:
        return False
    if os.path.isabs(rel):
        return False
    parts = rel.split("/")
    if any(part in ("", ".") for part in parts):
        return False
    if any(part == ".." for part in parts):
        return False
    if parts[0].startswith("~"):
        return False
    return True


def _is_runtime(rel: str) -> bool:
    if rel.startswith(RUNTIME_PREFIXES):
        return True
    if rel in RUNTIME_EXTRA_BASENAMES:
        return True
    return False


def _derive_build_id(source: Path) -> str:
    """Try to derive a 40-char Git commit SHA from ``source``."""
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


def _canonical_tarinfo(rel: str, size: int, mode: int) -> tarfile.TarInfo:
    """Build a deterministic, safe ``TarInfo`` for ``rel``.

    * strips uid/gid
    * normalizes mtime to 0
    * forces a safe mode (0o755 for dirs, 0o644 for files)
    * forces name to use forward slashes
    """
    ti = tarfile.TarInfo(name=rel)
    ti.size = size
    ti.mtime = 0
    ti.mode = mode
    ti.uid = 0
    ti.gid = 0
    ti.uname = ""
    ti.gname = ""
    ti.type = tarfile.DIRTYPE if (mode & 0o111) and size == 0 else tarfile.REGTYPE
    return ti


def _build_tarball(payload_root: Path, manifest_blob: bytes, tarball_path: Path) -> None:
    """Create a deterministic tarball that:

    * contains ``./bootstrap-manifest.json`` (the SAME blob that
      lives at the outer canonical path)
    * contains every runtime file under ``payload_root``
    * uses forward-slash paths relative to ``./``
    * refuses any symlink, device, FIFO, absolute path, or
      ``..`` traversal
    """
    # Build the sorted file list ahead of time so the tar order is
    # deterministic.
    files: list[tuple[str, Path]] = []
    for p in sorted(payload_root.rglob("*")):
        rel = p.relative_to(payload_root).as_posix()
        if p.is_symlink():
            if _is_runtime(rel):
                raise RuntimeError(f"REFUSED: runtime source contains symlink: {p}")
            continue
        if not p.is_file():
            continue
        if rel.endswith("__pycache__"):
            continue
        if rel.endswith((".pyc", ".wasm", ".map")):
            continue
        if "/.git/" in ("/" + rel):
            continue
        if not _safe_relpath(rel):
            raise RuntimeError(f"REFUSED: unsafe payload path: {rel!r}")
        if not _is_runtime(rel):
            continue
        files.append((rel, p))

    if tarball_path.exists():
        tarball_path.unlink()
    with tarfile.open(tarball_path, "w:gz", compresslevel=9) as tf:
        # Emit a top-level directory entry for clarity.
        ti = _canonical_tarinfo("./", 0, 0o755)
        ti.type = tarfile.DIRTYPE
        tf.addfile(ti)

        # Emit the manifest first so a tar -tzf is self-describing.
        ti = _canonical_tarinfo("./bootstrap-manifest.json", len(manifest_blob), 0o644)
        ti.type = tarfile.REGTYPE
        tf.addfile(ti, io.BytesIO(manifest_blob))

        seen: set[str] = set()
        seen.add("./bootstrap-manifest.json")
        for rel, p in files:
            arcname = "./" + rel
            if arcname in seen:
                continue
            seen.add(arcname)
            data = p.read_bytes()
            # Refuse accidental symlinks (some filesystems cannot
            # toggle lstat vs stat reliably; double-check).
            if (p.stat().st_mode & 0o170000) == 0o120000:
                raise RuntimeError(f"REFUSED: source symlink: {p}")
            ti = _canonical_tarinfo(arcname, len(data), 0o644)
            tf.addfile(ti, io.BytesIO(data))


def _build_manifest_blob(
    payload_root: Path,
    build_id: str,
) -> bytes:
    """Mirror build_bootstrap_manifest.py: schema v2 manifest of payload."""
    files: dict[str, str] = {}
    for p in sorted(payload_root.rglob("*")):
        if p.is_symlink():
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(payload_root).as_posix()
        if rel.endswith("__pycache__"):
            continue
        if rel in EXCLUDED_BASENAMES:
            continue
        if rel.endswith("bootstrap-manifest.json"):
            continue
        if "/.git/" in ("/" + rel):
            continue
        if rel.endswith((".pyc", ".wasm", ".map")):
            continue
        if not _safe_relpath(rel):
            raise RuntimeError(f"REFUSED: unsafe payload path: {rel!r}")
        if not _is_runtime(rel):
            continue
        files[rel] = _hash_file(p)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "build_id": build_id,
        "source_root": payload_root.name,
        "file_count": len(files),
        "hashes": dict(sorted(files.items())),
    }
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


def _verify_inner_manifest_matches_outer(outer_manifest: bytes, tarball: Path) -> None:
    """Open the tarball, extract the embedded bootstrap-manifest.json,
    and assert its bytes equal the outer manifest.  This is the
    explicit ortho-checksum that ensures the operator's outer manifest
    is the EXACT blob the inner verifier will consume.
    """
    with tarfile.open(tarball, "r:gz") as tf:
        member = None
        for ti in tf.getmembers():
            if ti.name == "./bootstrap-manifest.json":
                member = ti
                break
        if member is None:
            raise RuntimeError(
                "REFUSED: tarball does not contain ./bootstrap-manifest.json"
            )
        if member.type != tarfile.REGTYPE:
            raise RuntimeError(
                f"REFUSED: tarball member is not a regular file: {member.name}"
            )
        if not member.isfile():
            raise RuntimeError(f"REFUSED: tarball member is not a file: {member.name}")
        if not member.name.startswith("./"):
            raise RuntimeError(f"REFUSED: tarball member escapes root: {member.name}")
        inner = tf.extractfile(member).read()
    if inner != outer_manifest:
        raise RuntimeError(
            "REFUSED: outer manifest bytes differ from tarball embedded manifest"
        )


def _write_shasums(tarball: Path, manifest: Path, sums: Path) -> None:
    """Emit SHA256SUMS over the tarball AND the outer manifest."""
    lines: list[str] = []
    for label, p in (
        ("peer-deployer-package.tar.gz", tarball),
        ("bootstrap-manifest.json", manifest),
    ):
        h = _hash_file(p)
        lines.append(f"{h}  {label}\n")
    sums.write_text("".join(lines))


def _copy_wrapper(source_repo: Path, out_path: Path) -> None:
    """Copy the canonical bootstrap-installer.sh into the handoff.

    The wrapper is committed to the repo at
    ``deploy/scripts/bootstrap-installer.sh``.  We copy it into the
    handoff so the operator runs the exact committed wrapper, not a
    version that has been tampered with in flight.
    """
    src = source_repo / "deploy" / "scripts" / "bootstrap-installer.sh"
    if not src.is_file():
        raise RuntimeError(
            f"REFUSED: wrapper missing in source: {src}. Did you forget to "
            f"commit deploy/scripts/bootstrap-installer.sh?"
        )
    shutil.copy2(src, out_path)
    os.chmod(out_path, 0o755)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--source",
        type=Path,
        required=True,
        help="path to the exact checkout (filesystem root, never a URL)",
    )
    ap.add_argument(
        "--build-id",
        type=str,
        default=None,
        help="explicit 40-char Git commit SHA; default derived from source/.git",
    )
    ap.add_argument(
        "--output",
        type=Path,
        required=True,
        help="dest directory for the handoff (must NOT yet exist)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="allow overwrite of an existing --output",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv)
    if not ns.source.is_dir():
        sys.stderr.write(f"REFUSED: missing source: {ns.source}\n")
        return 2
    if ns.output.exists() or ns.output.is_symlink():
        if not ns.force:
            sys.stderr.write(
                f"REFUSED: output already exists: {ns.output} (use --force to overwrite)\n"
            )
            return 2
        shutil.rmtree(ns.output)

    if ns.build_id is not None:
        if not SHA_RE.fullmatch(ns.build_id):
            sys.stderr.write(
                f"REFUSED: --build-id must be a 40-char SHA: {ns.build_id!r}\n"
            )
            return 2
        build_id = ns.build_id
    else:
        build_id = _derive_build_id(ns.source)

    # Build empty handoff directory.
    out_dir = ns.output
    pkg_dir = out_dir / "peer-deployer-package"
    out_dir.mkdir(parents=True)
    pkg_dir.mkdir(parents=True)

    # 1. wrapper
    wrapper = out_dir / "bootstrap-installer.sh"
    _copy_wrapper(ns.source, wrapper)

    # 2. payload manifest (outer)
    manifest_outer = pkg_dir / "bootstrap-manifest.json"
    manifest_bytes = _build_manifest_blob(ns.source, build_id)
    manifest_outer.write_bytes(manifest_bytes)

    # 3. tarball (embedded manifest matches outer)
    tarball = pkg_dir / "peer-deployer-package.tar.gz"
    _build_tarball(ns.source, manifest_bytes, tarball)
    _verify_inner_manifest_matches_outer(manifest_bytes, tarball)

    # 4. SHA256SUMS
    sums = pkg_dir / "SHA256SUMS"
    _write_shasums(tarball, manifest_outer, sums)

    # 5. PACKAGE.json
    package_blob = {
        "schema": PACKAGE_LAYOUT_VERSION,
        "build_id": build_id,
        "builder": "deploy/scripts/build_control_room_peer_deployer_bootstrap.py",
        "builder_version": "1",
        "manifest_schema": MANIFEST_SCHEMA,
        "source_repo": str(ns.source.resolve()),
        "source_root": ns.source.name,
        "layout": {
            "wrapper": "bootstrap-installer.sh",
            "package_dir": "peer-deployer-package/",
            "artifacts": [
                "peer-deployer-package/peer-deployer-package.tar.gz",
                "peer-deployer-package/bootstrap-manifest.json",
                "peer-deployer-package/PACKAGE.json",
                "peer-deployer-package/SHA256SUMS",
            ],
        },
        "sha256": {
            "peer-deployer-package.tar.gz": _hash_file(tarball),
            "bootstrap-manifest.json": _hash_file(manifest_outer),
        },
        "expected_outer_paths": list(REQUIRED_OUTER_FILES),
        "generated_at": int(time.time()),
    }
    (pkg_dir / "PACKAGE.json").write_text(
        json.dumps(package_blob, indent=2, sort_keys=True)
    )

    # Sanity: every required outer path is a regular file (not a symlink).
    for rel in REQUIRED_OUTER_FILES:
        p = out_dir / rel
        if p.is_symlink():
            raise RuntimeError(f"REFUSED: produced symlink: {p}")
        if not p.is_file():
            raise RuntimeError(f"REFUSED: missing required file: {p}")

    print(
        f"wrote handoff at {out_dir} (build_id={build_id[:12]}, "
        f"tarball={tarball.stat().st_size} bytes, manifest={len(manifest_bytes)} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
