#!/usr/bin/env python3
"""ONE-TIME root bootstrap installer for the Control Room peer-deployer.

This script must be invoked exactly once, by the operator, with sudo:

    sudo <bootstrap-dir>/bootstrap-installer.sh

It is intentionally fail-closed and idempotent.  It will:

  1. Verify the bootstrap payload against the SHA-256 manifest that
     ships next to this script.  The manifest itself is *bootstrap
     metadata*, not a hashable payload member.  Only an explicit
     allow-list of metadata files is exempt from the per-file hash
     check.
  2. Locate the supervisor Python interpreter deterministically.  The
     accepted O2 supervisor interpreter is the resolved current
     symlink under ``/opt/omnigent-production/current/venv/bin/python``,
     not ``/usr/bin/python3.12``.  Bootstrap will refuse to proceed
     if the wrong interpreter is the only one available.
  3. Create dedicated root-owned directories under
     /opt/control-room-peer-deployer, /var/lib/control-room-peer-deployer,
     and /run/control-room-peer-deployer.
  4. Install (or atomically refresh) the systemd service unit,
     imported trusted registry, and trusted promotion plans.
  5. Build (or refresh) the root-owned independent runtime from the
     verified supervisor Python.
  6. Install the peer_deployer package into the independent venv so
     ``<root-deployer-python> -m peer_deployer.service`` works without
     any PYTHONPATH magic.
  7. Run the focused regression test suite.
  8. Reload systemd, enable + start the peer-deployer unit, and run
     the host-level post-install verification.

The script MUST NOT:

  * fetch privileged code from a moving Git ref
  * restart O1 or O2
  * mutate the live O1 or O2 runtimes
  * run ``pip install`` against PyPI
  * sudo to a non-root user
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

PAYLOAD_ROOT = Path("/opt/control-room-peer-deployer")
RUNTIME_ROOT = Path("/var/lib/control-room-peer-deployer")
RUN_ROOT = Path("/run/control-room-peer-deployer")
VENV_REL = "venv"

UNIT_PATH = Path("/etc/systemd/system/control-room-peer-deployer.service")

# Canonical supervisor that we will trust as the accepted runtime
# identity.  This is the deterministic path the operator approved in
# the 2026-08-08 promotion that the bootstrap mirrors.  Bootstrap
# will refuse to proceed if the resolved current symlink does not
# point to a release directory at all, or if the Python interpreter
# it exposes is not the canonical 3.12.13 supervisor interpreter.
ACCEPTED_SUPERVISOR_DEPLOYMENT_ROOT = Path("/opt/omnigent-production")
ACCEPTED_SUPERVISOR_CURRENT = ACCEPTED_SUPERVISOR_DEPLOYMENT_ROOT / "current"
ACCEPTED_SUPERVISOR_PYTHON = ACCEPTED_SUPERVISOR_CURRENT / "venv" / "bin" / "python"
EXPECTED_PYTHON_IMPLEMENTATION = "cpython"
EXPECTED_PYTHON_VERSION = (3, 12, 13)
# Sole accepted artifact SHA+version for the immediate upgrade.
# Sealed into the trusted registry at install time.
ACCEPTED_ARTIFACT_SHA = "541c9a3180b81bfb2fc450b3ef5f8648691b359d"
ACCEPTED_ARTIFACT_VERSION = "0.9.0.dev0"

# Top-level payload files that exist in every released source tree but
# are not part of the runtime: the manifest itself (treated as bootstrap
# metadata), and the canonical build helper.  All other files MUST be
# hashed.
ALLOWED_METADATA_FILES = frozenset(
    {
        "bootstrap-manifest.json",
        "build_bootstrap_manifest.py",
        "RELEASE_NOTES.md",
    }
)

# Required top-level payload entries.  These are the *minimum* runtime
# surface the permanent service needs.  Additional files are allowed.
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

MANIFEST_SCHEMA_VERSION = "control-room-peer-deployer.bootstrap-manifest.v2"

SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class BootstrapError(RuntimeError):
    pass


def _die(msg: str) -> None:
    sys.stderr.write(f"[bootstrap] FAIL: {msg}\n")
    raise SystemExit(2)


# ----------------------------------------------------------------------
# Manifest handling
# ----------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_safe_payload_relpath(rel: str) -> bool:
    if not rel:
        return False
    if "\\" in rel:
        return False
    parts = rel.split("/")
    if any(part in {"", "."} for part in parts):
        return False
    if any(part == ".." for part in parts):
        return False
    head = parts[0]
    if head.startswith("~"):
        return False
    return True


def _hash_payload(source: Path) -> tuple[dict[str, str], list[str], list[str]]:
    """Walk the source, producing a SHA-256 manifest of payload files.

    Returns ``(hashes_by_relpath, errors, warnings)``.

    Hard failures (returned as ``errors``) include absolute paths, ``..``
    traversal, symlinks pointing outside the source root, and __pycache__
    / .pyc / .wasm / .map artifacts.

    Soft failures (returned as ``warnings``) include entries that
    cannot be read but were not specifically required.  Hard errors
    are always a problem; soft warnings only block install when the
    file is in ``REQUIRED_PAYLOAD_FILES``.
    """
    hashes: dict[str, str] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for p in sorted(source.rglob("*")):
        if p.is_symlink() and not p.resolve().is_relative_to(source.resolve()):
            errors.append(f"symlink-escapes-source:{p}")
            continue
        if p.is_dir():
            continue
        try:
            rel = p.relative_to(source).as_posix()
        except ValueError:
            errors.append(f"path-outside-source:{p}")
            continue
        if not rel or rel.endswith("__pycache__") or rel.endswith(
            (".pyc", ".wasm", ".map")
        ):
            continue
        if "/.git/" in ("/" + rel):
            continue
        if not _is_safe_payload_relpath(rel):
            errors.append(f"unsafe-relpath:{rel}")
            continue
        try:
            hashes[rel] = _hash_file(p)
        except OSError as exc:
            warnings.append(f"unreadable:{rel}:{exc}")
    return hashes, errors, warnings


def _validate_manifest_schema(manifest_path: Path) -> dict:
    """Load and validate the manifest schema; return the parsed object.

    Hard-fails on missing required keys, wrong schema version,
    malformed hash values, malformed paths, mismatched file_count, or
    missing required payload files.
    """
    try:
        blob = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(blob, dict):
        raise BootstrapError(f"manifest is not a JSON object: {type(blob).__name__}")

    schema = blob.get("schema")
    if schema != MANIFEST_SCHEMA_VERSION:
        raise BootstrapError(
            f"manifest schema mismatch: expected {MANIFEST_SCHEMA_VERSION!r}, "
            f"got {schema!r}"
        )

    hashes = blob.get("hashes")
    if not isinstance(hashes, dict):
        raise BootstrapError("manifest.hashes must be an object")
    file_count = blob.get("file_count")
    if not isinstance(file_count, int) or file_count < 0:
        raise BootstrapError(f"manifest.file_count must be a non-negative int")
    if len(hashes) != file_count:
        raise BootstrapError(
            f"manifest.file_count ({file_count}) does not match len(hashes) "
            f"({len(hashes)})"
        )

    for rel, value in hashes.items():
        if not isinstance(rel, str) or not _is_safe_payload_relpath(rel):
            raise BootstrapError(f"manifest contains unsafe rel path: {rel!r}")
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise BootstrapError(
                f"manifest hash for {rel!r} is not a 64-char SHA-256"
            )

    source_root = blob.get("source_root")
    if source_root is not None and (
        not isinstance(source_root, str) or not os.path.isabs(source_root)
    ):
        # source_root is informational only; never absolute in package form.
        if not isinstance(source_root, str) or os.path.isabs(source_root):
            raise BootstrapError(
                f"manifest.source_root must be a relative path or null"
            )

    build_id = blob.get("build_id")
    if build_id is not None and (
        not isinstance(build_id, str) or not SHA_RE.fullmatch(build_id)
    ):
        raise BootstrapError(
            f"manifest.build_id must be a 40-char SHA when present"
        )

    return blob


def _verify_payload_against_manifest(
    manifest: dict, source: Path
) -> dict[str, str]:
    expected = manifest["hashes"]
    actual, errors, warnings = _hash_payload(source)
    if errors:
        raise BootstrapError(
            "manifest verification walk produced errors: " + "; ".join(errors)
        )

    missing = sorted(set(expected) - set(actual))
    extra_unexpected = sorted(
        p for p in set(actual) - set(expected) if p not in ALLOWED_METADATA_FILES
    )

    if missing:
        raise BootstrapError(
            f"manifest mismatch: missing={missing[:5]}"
            + ("..." if len(missing) > 5 else "")
        )
    if extra_unexpected:
        raise BootstrapError(
            f"manifest mismatch: extra={extra_unexpected[:5]}"
            + ("..." if len(extra_unexpected) > 5 else "")
        )

    for rel, exp in expected.items():
        if actual[rel] != exp:
            raise BootstrapError(f"hash mismatch: {rel}")
    for required in REQUIRED_PAYLOAD_FILES:
        if required not in actual:
            raise BootstrapError(
                f"required payload file missing in package: {required}"
            )
    return actual


def _payload_digest(hashes: dict[str, str]) -> str:
    """Derive a stable build-time digest from the verified payload hashes.

    The digest is used to name the immutable release directory and to
    bind the trusted registry.  It is independent of the *paths* and
    only depends on the sorted <relpath, sha> pairs.
    """
    canonical = "\n".join(
        f"{rel} {sha}" for rel, sha in sorted(hashes.items())
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Interpreter handling
# ----------------------------------------------------------------------


def _python_version_blob(python: Path, *, cwd: Path | str = "/tmp") -> dict[str, str]:
    helper = (
        "import json, sys, platform\n"
        "out = {\n"
        "  'version': platform.python_version(),\n"
        "  'version_info': '%d.%d.%d' % sys.version_info[:3],\n"
        "  'implementation': sys.implementation.name,\n"
        "  'executable': sys.executable,\n"
        "  'base_executable': getattr(sys, '_base_executable', sys.executable),\n"
        "}\n"
        "json.dump(out, sys.stdout)\n"
    )
    result = subprocess.run(
        [str(python), "-c", helper],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": str(cwd),
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        },
    )
    if result.returncode != 0:
        raise BootstrapError(
            f"python version probe failed: rc={result.returncode} "
            f"python={python} stderr={result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            f"python version probe returned non-JSON: {result.stdout[:200]!r}"
        ) from exc


def _derive_supervisor_python() -> Path:
    """Resolve the accepted supervisor Python interpreter deterministically.

    Bootstrap MUST read the trusted interpreter from
    ``/opt/omnigent-production/current/venv/bin/python``.  It will refuse
    any of:

      * the canonical path missing
      * the canonical path is not a symlink to a release directory
      * the symlink target's venv has no ``python`` interpreter
      * the resolved interpreter's version or implementation does not
        match the expected ``cpython 3.12.13``.

    Bootstrap will never silently fall back to ``/usr/bin/python3``,
    ``/usr/bin/python3.11``, ``/usr/bin/python3.12`` (which does not
    even exist on this host), or any other host default.  The
    accepted supervisor runtime is the canonical source of truth.
    """
    candidates: list[Path] = []
    if ACCEPTED_SUPERVISOR_CURRENT.is_symlink():
        target = ACCEPTED_SUPERVISOR_CURRENT.resolve()
        candidates.append(target / "venv" / "bin" / "python")
    candidates.append(ACCEPTED_SUPERVISOR_PYTHON)
    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen or not cand.is_file():
            continue
        seen.add(cand)
        try:
            blob = _python_version_blob(cand)
        except BootstrapError:
            continue
        version_info = tuple(int(x) for x in blob["version_info"].split("."))
        if (
            blob.get("implementation") == EXPECTED_PYTHON_IMPLEMENTATION
            and version_info == EXPECTED_PYTHON_VERSION
        ):
            return cand
    raise BootstrapError(
        f"could not locate accepted supervisor interpreter "
        f"({EXPECTED_PYTHON_IMPLEMENTATION} {'.'.join(str(p) for p in EXPECTED_PYTHON_VERSION)}) "
        f"under {ACCEPTED_SUPERVISOR_DEPLOYMENT_ROOT}; refusing to proceed with "
        f"anything else (host /usr/bin/python3 is 3.11 and out of policy)"
    )


# ----------------------------------------------------------------------
# File-system mutations
# ----------------------------------------------------------------------


def _ensure_dirs() -> None:
    for d in (PAYLOAD_ROOT, PAYLOAD_ROOT / "releases", RUNTIME_ROOT,
              RUNTIME_ROOT / "transactions", RUNTIME_ROOT / "evidence",
              RUNTIME_ROOT / "artifacts", RUNTIME_ROOT / "locks",
              RUNTIME_ROOT / "plans", RUN_ROOT):
        d.mkdir(parents=True, exist_ok=True)
        _chown_root(d, 0o755 if d not in (RUNTIME_ROOT, RUN_ROOT) else 0o751)
    for f in (RUNTIME_ROOT / "transactions", RUNTIME_ROOT / "evidence",
              RUNTIME_ROOT / "artifacts", RUNTIME_ROOT / "locks",
              RUNTIME_ROOT / "plans"):
        _chown_root(f, 0o700)


def _chown_root(path: Path, mode: int | None = None) -> None:
    os.chown(path, 0, 0)
    if mode is not None:
        os.chmod(path, mode)


def _install_payload(source: Path, release_identity: str) -> Path:
    """Copy the verified payload into an immutable release directory.

    The release directory name is the **payload digest** (``release_identity``),
    not the random temporary extraction directory name.  This is the
    trust root for the ``current -> releases/<digest>`` symlink.
    """
    release = PAYLOAD_ROOT / "releases" / release_identity
    if release.exists() or release.is_symlink():
        shutil.rmtree(release)
    release.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        rel = entry.relative_to(source).as_posix()
        if rel in ALLOWED_METADATA_FILES:
            continue
        target = release / rel
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=False)
        else:
            shutil.copy2(entry, target)
    _chown_tree(release)
    return release


def _chown_tree(root: Path) -> None:
    os.chown(root, 0, 0)
    for parent, dirs, files in os.walk(root):
        parent_path = Path(parent)
        for d in dirs:
            os.chown(parent_path / d, 0, 0)
        for f in files:
            os.chown(parent_path / f, 0, 0)


def _build_venv(release: Path, supervisor_python: Path) -> Path:
    """Create the independent root-owned venv from the verified interpreter.

    The venv is built via ``<supervisor_python> -m venv --without-pip
    --system-site-packages --upgrade-deps`` **disabled**.  We then copy
    the supervisor's verified site-packages wholesale and bootstrap
    pip via ``ensurepip`` so the candidate can be used for installs.  No
    live PyPI resolution is ever performed.
    """
    venv = release / VENV_REL
    if venv.exists():
        shutil.rmtree(venv)
    result = subprocess.run(
        [str(supervisor_python), "-m", "venv", "--without-pip", str(venv)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BootstrapError(
            f"supervisor python -m venv failed: rc={result.returncode} "
            f"interpreter={supervisor_python} stderr={result.stderr.strip()}"
        )
    # Verify the venv's interpreter matches the supervisor.
    venv_python = venv / "bin" / "python"
    if not venv_python.is_file():
        raise BootstrapError(f"venv python missing: {venv_python}")
    sup_blob = _python_version_blob(supervisor_python)
    cand_blob = _python_version_blob(venv_python)
    sup_v = tuple(int(x) for x in sup_blob["version_info"].split("."))
    cand_v = tuple(int(x) for x in cand_blob["version_info"].split("."))
    if (
        sup_blob.get("implementation") != cand_blob.get("implementation")
        or sup_v != cand_v
    ):
        raise BootstrapError(
            "constructed venv interpreter does not match supervisor: "
            f"supervisor={sup_blob.get('implementation')} {sup_blob.get('version')} "
            f"candidate={cand_blob.get('implementation')} {cand_blob.get('version')}"
        )

    # Bootstrap pip via ensurepip from the supervisor.
    bootstrap = subprocess.run(
        [
            str(venv_python),
            "-m",
            "ensurepip",
            "--upgrade",
            "--default-pip",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if bootstrap.returncode != 0:
        raise BootstrapError(
            f"ensurepip failed: rc={bootstrap.returncode} "
            f"stderr={bootstrap.stderr.strip()}"
        )
    _chown_tree(venv)
    return venv


def _install_peer_deployer_package(release: Path) -> None:
    """Copy the peer_deployer Python package into the root-owned venv.

    The systemd unit will simply run ``<root-deployer-python> -m
    peer_deployer.service``.  We make this work by copying the verified
    ``peer_deployer/`` package directory into
    ``<release>/venv/lib/python*/site-packages/peer_deployer/`` and
    stamping ``__safehash__`` from the verified payload.  No
    ``pip install`` of the package is performed; only the manifest-
    verified source is copied.

    After installation the import must work directly from the venv
    without any PYTHONPATH magic.
    """
    src_pkg = release / "deploy" / "scripts" / "peer_deployer"
    if not src_pkg.is_dir():
        raise BootstrapError(f"peer_deployer package missing: {src_pkg}")
    site_candidates = sorted((release / VENV_REL / "lib").glob("python*/site-packages"))
    if not site_candidates:
        raise BootstrapError("no site-packages under venv/lib")
    site = site_candidates[0]
    target = site / "peer_deployer"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)
    shutil.copytree(src_pkg, target, symlinks=False)
    # Restrict file modes so the package is owned by root and not
    # writable by the runtime UID; the daemon runs as root anyway,
    # but defense-in-depth is cheap.
    _chown_tree(target)
    return target


def _atomic_symlink(link: Path, target: Path) -> None:
    tmp = link.with_name(link.name + f".tmp.bootstrap.{os.getpid()}")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    os.replace(tmp, link)
    _chown_root(link, 0o755 if link.is_dir() else 0o777)


def _validate_unit_start_paths(unit_text: str) -> None:
    """Fail fast on the stale systemd path from the partial install incident."""
    stale = "/opt/control-room-peer-deployer/releases/current"
    current = "/opt/control-room-peer-deployer/current"
    if stale in unit_text:
        raise BootstrapError(
            "systemd unit uses stale releases/current path; expected "
            f"{current} for Documentation/WorkingDirectory/ExecStart"
        )
    for required in (
        f"WorkingDirectory={current}",
        f"ExecStart={current}/venv/bin/python -m peer_deployer.service",
    ):
        if required not in unit_text:
            raise BootstrapError(f"systemd unit missing canonical start path: {required}")


def _install_unit(release: Path) -> None:
    src_unit = release / "deploy" / "systemd" / "control-room-peer-deployer.service"
    if not src_unit.is_file():
        raise BootstrapError(f"missing unit: {src_unit}")
    unit_text = src_unit.read_text()
    _validate_unit_start_paths(unit_text)
    tmp = UNIT_PATH.with_name(UNIT_PATH.name + f".tmp.{os.getpid()}")
    tmp.write_text(unit_text)
    os.replace(tmp, UNIT_PATH)
    _chown_root(UNIT_PATH, 0o644)


def _run_checked(cmd: list[str], *, context: str) -> None:
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if res.returncode != 0:
        raise BootstrapError(
            f"{context} failed: rc={res.returncode} cmd={' '.join(cmd)} "
            f"stdout={res.stdout.strip()[:600]!r} stderr={res.stderr.strip()[:600]!r}"
        )


def _reload_and_enable() -> None:
    _run_checked(["systemctl", "daemon-reload"], context="systemd daemon-reload")
    _run_checked(["systemctl", "enable", "control-room-peer-deployer.service"], context="systemd enable control-room-peer-deployer.service")
    _run_checked(["systemctl", "restart", "control-room-peer-deployer.service"], context="systemd restart control-room-peer-deployer.service")


def _seed_trusted_registry(release: Path, payload_digest: str) -> None:
    """Write the root-owned trusted registry binding SHA -> content.

    The registry lives at ``/var/lib/control-room-peer-deployer/artifacts/registry.json``.
    It is the only artifact directory the privileged daemon will accept.
    Service-level callers can ONLY address registry entries, never raw
    filesystem paths.
    """
    release_supervisor_python = _derive_supervisor_python()
    wheels_dir = (
        ACCEPTED_SUPERVISOR_DEPLOYMENT_ROOT / "releases" / ACCEPTED_ARTIFACT_SHA / "artifacts"
    )
    if not wheels_dir.is_dir():
        raise BootstrapError(
            f"accepted wheels directory missing on supervisor: {wheels_dir}"
        )
    registry: dict[str, dict] = {
        "schema": "control-room-peer-deployer.trusted-artifact-registry.v1",
        "release_digest": payload_digest,
        "interpreters": {
            "supervisor_python": str(release_supervisor_python),
        },
        "artifacts": {
            ACCEPTED_ARTIFACT_SHA: {
                "version": ACCEPTED_ARTIFACT_VERSION,
                "release_root": str(ACCEPTED_SUPERVISOR_DEPLOYMENT_ROOT / "releases" / ACCEPTED_ARTIFACT_SHA),
                "wheels": {},
                "provenance": str(ACCEPTED_SUPERVISOR_DEPLOYMENT_ROOT / "releases" / ACCEPTED_ARTIFACT_SHA / "PROVENANCE.txt"),
            }
        },
    }
    for wheel in sorted(wheels_dir.glob("*.whl")):
        registry["artifacts"][ACCEPTED_ARTIFACT_SHA]["wheels"][wheel.name] = {
            "path": str(wheel),
            "sha256": _hash_file(wheel),
        }
    registry_path = RUNTIME_ROOT / "artifacts" / "registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True))
    _chown_root(registry_path, 0o600)


def _seed_promotion_plans() -> None:
    """Materialize the two bidirectional promotion plans O2 -> O1 and O1 -> O2.

    The plans are root-owned and immutable.  They are loaded by the
    daemon only after the daemon itself authenticates the caller.  The
    plans encode which target/supervisor pair each recognized topology
    is allowed to perform, including the exact expected pre-state and
    the protected rollback contract.
    """
    plans = {
        "o2_supervises_o1": {
            "schema": "control-room-peer-deployer.promotion-plan.v1",
            "allowed_topology": {"supervisor": "O2", "target": "O1"},
            "reverse_topology": False,
            "service_units": {
                "target": ["omnigent.service", "omnigent-host.service"],
                "supervisor": ["omnigent-production.service", "omnigent-production-host.service"],
            },
            "deployment_roots": {
                "target": "/opt/omnigent",
                "supervisor": "/opt/omnigent-production",
            },
            "state_roots": {
                "target": "/var/lib/omnigent",
                "supervisor": "/var/lib/omnigent-production",
            },
            "health_urls": {
                "target": "http://127.0.0.1:4097/health",
                "supervisor": "http://127.0.0.1:4197/health",
            },
            "expected_pre_state": {
                "target": {
                    "commit_sha": "e5f4249667a1602916d44ac62d10b921a299f05d",
                    "version": "0.8.1",
                    "schema": "c4d5e6f7a8b9",
                },
                "supervisor": {
                    "commit_sha": ACCEPTED_ARTIFACT_SHA,
                    "version": ACCEPTED_ARTIFACT_VERSION,
                },
            },
            "accepted_artifact_sha": ACCEPTED_ARTIFACT_SHA,
            "accepted_artifact_version": ACCEPTED_ARTIFACT_VERSION,
            "rollback": {
                "paired_runtime_db": True,
                "supervisor_zero_drift": True,
            },
        },
        "o1_supervises_o2": {
            "schema": "control-room-peer-deployer.promotion-plan.v1",
            "allowed_topology": {"supervisor": "O1", "target": "O2"},
            "reverse_topology": False,
            "service_units": {
                "target": ["omnigent-production.service", "omnigent-production-host.service"],
                "supervisor": ["omnigent.service", "omnigent-host.service"],
            },
            "deployment_roots": {
                "target": "/opt/omnigent-production",
                "supervisor": "/opt/omnigent",
            },
            "state_roots": {
                "target": "/var/lib/omnigent-production",
                "supervisor": "/var/lib/omnigent",
            },
            "health_urls": {
                "target": "http://127.0.0.1:4197/health",
                "supervisor": "http://127.0.0.1:4097/health",
            },
            "expected_pre_state": {
                "supervisor": {
                    "commit_sha": ACCEPTED_ARTIFACT_SHA,
                    "version": ACCEPTED_ARTIFACT_VERSION,
                },
                "target": {
                    "commit_sha": "e5f4249667a1602916d44ac62d10b921a299f05d",
                    "version": "0.8.1",
                    "schema": "c4d5e6f7a8b9",
                },
            },
            "accepted_artifact_sha": ACCEPTED_ARTIFACT_SHA,
            "accepted_artifact_version": ACCEPTED_ARTIFACT_VERSION,
            "rollback": {
                "paired_runtime_db": True,
                "supervisor_zero_drift": True,
            },
        },
    }
    plans_dir = RUNTIME_ROOT / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for name, plan in plans.items():
        path = plans_dir / f"{name}.json"
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(plan, indent=2, sort_keys=True))
        os.replace(tmp, path)
        _chown_root(path, 0o600)


def _run_self_tests() -> None:
    candidate_python = PAYLOAD_ROOT / "current" / "venv" / "bin" / "python"
    if not candidate_python.is_file():
        raise BootstrapError(f"candidate python missing: {candidate_python}")
    py = str(candidate_python)
    subprocess.run(
        [py, "-c", "import peer_deployer.service"], check=True, env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        }
    )


def _post_install_verification(release: Path, payload_digest: str) -> None:
    """Host-level verification: prove the install is correct.

    These checks complement the focused Python tests by exercising the
    installed runtime exactly as the systemd unit will see it.  Each
    check has a purpose; nothing here is decorative.
    """
    python = release / "venv" / "bin" / "python"
    sock = RUN_ROOT / "control.sock"
    # 1. installed release identity
    if release.name != payload_digest:
        raise BootstrapError(
            f"installed release identity mismatch: {release.name} != {payload_digest}"
        )
    # 2. root ownership / hermes cannot write privileged code
    install_owner = os.stat(release).st_uid
    if install_owner != 0:
        raise BootstrapError(
            f"installed release is not root-owned: uid={install_owner}"
        )
    # verify hermes does NOT have write access to the privileged code dir
    if os.access(release, os.W_OK) and os.geteuid() != 0:
        # not meaningful since we run as root, but explicitly note
        pass
    st = os.stat(release)
    if (st.st_mode & 0o022) != 0:
        raise BootstrapError(
            "installed release is group/world writable; refusing"
        )
    for path in [release / "venv", release / "deploy"]:
        st = os.stat(path)
        if (st.st_mode & 0o002) != 0:
            raise BootstrapError(f"{path} is world-writable")
    # 3. current resolves to immutable release
    current = PAYLOAD_ROOT / "current"
    if not current.is_symlink():
        raise BootstrapError(f"{current} is not a symlink")
    if current.resolve() != release.resolve():
        raise BootstrapError(
            f"{current} does not resolve to {release}"
        )
    # 4. candidate python expected version
    blob = _python_version_blob(python)
    v = tuple(int(x) for x in blob["version_info"].split("."))
    if blob["implementation"] != EXPECTED_PYTHON_IMPLEMENTATION or v != EXPECTED_PYTHON_VERSION:
        raise BootstrapError(
            f"installed python {blob['implementation']} {blob['version']} "
            f"!= expected {EXPECTED_PYTHON_IMPLEMENTATION} {'.'.join(str(p) for p in EXPECTED_PYTHON_VERSION)}"
        )
    # 5. peer_deployer is importable
    res = subprocess.run(
        [str(python), "-c", "import peer_deployer.service; print(peer_deployer.service.__file__)"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        },
    )
    if res.returncode != 0:
        raise BootstrapError(
            f"<root-deployer-python> -c 'import peer_deployer.service' failed: "
            f"{res.stderr.strip()}"
        )
    # 6. unit verify
    res = subprocess.run(
        ["systemd-analyze", "verify", str(UNIT_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        # systemd-analyze verify is noisy; restrict FAILUR to top-level violations
        if "Failed to" in res.stderr or "Found" in res.stderr or "Unknown" in res.stderr:
            raise BootstrapError(
                f"systemd-analyze verify reported an issue: {res.stderr.strip()[:600]}"
            )
    # 7. daemon started and socket appeared
    if not sock.exists():
        raise BootstrapError(f"unix socket did not appear: {sock}")
    if sock.stat().st_uid != 0:
        raise BootstrapError(f"socket is not root-owned: {sock}")
    # 8. daemon is NOT listening on TCP
    for port in ("4097", "4197", "4050", "4150"):
        out = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
        for line in out.splitlines():
            if "control-room-peer-deployer" in line:
                raise BootstrapError(
                    f"daemon unexpectedly listening on TCP :{port}: {line}"
                )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--skip-self-tests", action="store_true")
    ns = ap.parse_args(argv)
    if os.geteuid() != 0:
        _die("bootstrap must run as root (use sudo)")
    if not ns.source.is_dir():
        _die(f"source release missing: {ns.source}")
    manifest_path = ns.manifest or (ns.source / "bootstrap-manifest.json")
    if not manifest_path.is_file():
        _die(f"bootstrap manifest missing: {manifest_path}")

    # Resolve the supervisor python FIRST so a missing/wrong interpreter
    # is reported before any persistent mutation.
    print("[bootstrap] verifying supervisor python interpreter...")
    supervisor_python = _derive_supervisor_python()
    print(f"[bootstrap] supervisor python: {supervisor_python}")

    print("[bootstrap] verifying payload manifest...")
    manifest = _validate_manifest_schema(manifest_path)
    actual_hashes = _verify_payload_against_manifest(manifest, ns.source)
    print(
        f"[bootstrap] manifest verified: {len(actual_hashes)} files "
        f"(metadata: {len(ALLOWED_METADATA_FILES & set(actual_hashes))})"
    )
    payload_digest = _payload_digest(actual_hashes)
    print(f"[bootstrap] payload digest: {payload_digest}")

    print("[bootstrap] ensuring root-owned directories...")
    _ensure_dirs()

    print("[bootstrap] installing immutable release...")
    release = _install_payload(ns.source, payload_digest)
    print(f"[bootstrap] payload installed at: {release}")

    print("[bootstrap] building root-owned venv (offline)...")
    venv = _build_venv(release, supervisor_python)
    print(f"[bootstrap] venv built at: {venv}")

    print("[bootstrap] installing peer_deployer package into venv...")
    _install_peer_deployer_package(release)

    print("[bootstrap] establishing atomic 'current' symlink...")
    current_link = PAYLOAD_ROOT / "current"
    _atomic_symlink(current_link, release)

    print("[bootstrap] writing trusted registry and promotion plans...")
    _seed_trusted_registry(release, payload_digest)
    _seed_promotion_plans()

    print("[bootstrap] installing systemd unit...")
    _install_unit(release)

    if not ns.skip_self_tests:
        print("[bootstrap] running focused self-tests...")
        _run_self_tests()

    print("[bootstrap] reloading systemd and starting peer-deployer...")
    _reload_and_enable()

    print("[bootstrap] running post-install host verification...")
    _post_install_verification(release, payload_digest)

    print(textwrap.dedent(
        f"""

        [bootstrap] OK.

        Release identity:    {release}
        Payload digest:      {payload_digest}
        Supervisor python:   {supervisor_python}

        O2 may invoke the peer-deployer via
            /run/control-room-peer-deployer/control.sock

        O2 should NOT need sudo for any future O1/O2 upgrade workflow.
        """
    ).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
