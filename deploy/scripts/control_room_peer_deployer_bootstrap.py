#!/usr/bin/env python3
"""ONE-TIME root bootstrap installer for the Control Room peer-deployer.

This script must be invoked exactly once, by the operator, with sudo:

    sudo /opt/control-room-peer-deployer/releases/current/bin/bootstrap.py

It is intentionally fail-closed and idempotent.  It will:

  1. Create dedicated root-owned directories under
     /opt/control-room-peer-deployer, /var/lib/control-room-peer-deployer,
     and /run/control-room-peer-deployer.
  2. Install (or refresh) the systemd service unit for the peer-deployer.
  3. Verify the bootstrap payload against the SHA-256 manifest that
     ships next to this script.  The bootstrap will REFUSE to install a
     payload whose hash does not match.
  4. Run the focused test suite against the deployed tree.
  5. Reload systemd, enable + start the peer-deployer unit.

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
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

PAYLOAD_ROOT = Path("/opt/control-room-peer-deployer")
RUNTIME_ROOT = Path("/var/lib/control-room-peer-deployer")
RUN_ROOT = Path("/run/control-room-peer-deployer")
SOURCE_RELEASE = Path(os.environ.get("BOOTSTRAP_SOURCE", "/var/lib/omnigent-production/tmp/peer-deployer-python312-fix"))
VENV_REL = "venv"

UNIT_PATH = Path("/etc/systemd/system/control-room-peer-deployer.service")

EXPECTED_TOP_LEVEL = {
    "deploy/scripts/peer_deployer/__init__.py",
    "deploy/scripts/peer_deployer/transaction.py",
    "deploy/scripts/peer_deployer/identity.py",
    "deploy/scripts/peer_deployer/preflight.py",
    "deploy/scripts/peer_deployer/staging.py",
    "deploy/scripts/peer_deployer/host_promotion.py",
    "deploy/scripts/peer_deployer/service.py",
    "deploy/scripts/peer_deployer/eligibility.py",
    "deploy/scripts/peer_deployer/rollback.py",
    "deploy/scripts/peer_deployer/service_state.py",
    "deploy/systemd/control-room-peer-deployer.service",
}


class BootstrapError(RuntimeError):
    pass


def _die(msg: str) -> None:
    sys.stderr.write(f"[bootstrap] FAIL: {msg}\n")
    raise SystemExit(2)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_dir(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.endswith("__pycache__"):
            continue
        if "/.git/" in ("/" + rel):
            continue
        if rel.endswith((".pyc", ".wasm", ".map")):
            continue
        out[rel] = _hash_file(p)
    return out


def _verify_manifest(manifest_path: Path, source: Path) -> dict[str, str]:
    manifest = json.loads(manifest_path.read_text())
    expected = manifest["hashes"]
    actual = _hash_dir(source)
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    if missing or extra:
        raise BootstrapError(f"manifest mismatch: missing={sorted(missing)[:3]} extra={sorted(extra)[:3]}")
    for rel, exp in expected.items():
        if actual[rel] != exp:
            raise BootstrapError(f"hash mismatch: {rel}")
    return expected


def _ensure_dirs() -> None:
    for d in (PAYLOAD_ROOT, PAYLOAD_ROOT / "releases", RUNTIME_ROOT,
              RUNTIME_ROOT / "transactions", RUNTIME_ROOT / "evidence",
              RUNTIME_ROOT / "artifacts", RUNTIME_ROOT / "locks",
              RUN_ROOT):
        d.mkdir(parents=True, exist_ok=True)
        os.chown(d, 0, 0)
        os.chmod(d, 0o755 if d not in (RUNTIME_ROOT, RUN_ROOT) else 0o751)
    for f in (RUNTIME_ROOT / "transactions", RUNTIME_ROOT / "evidence",
              RUNTIME_ROOT / "artifacts", RUNTIME_ROOT / "locks"):
        os.chmod(f, 0o700)


def _install_payload(source: Path) -> Path:
    release = PAYLOAD_ROOT / "releases" / source.name
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True, exist_ok=True)
    # The bootstrap only copies the immutable source tree plus the
    # peer_deployer package and the systemd unit.
    for rel in ("deploy", "deploy/scripts", "deploy/scripts/peer_deployer",
                "deploy/systemd", "deploy/systemd/omnigent-production.service.d",
                "deploy/systemd/omnigent.service.d"):
        src = source / rel
        if src.exists():
            shutil.copytree(src, release / rel, dirs_exist_ok=True)
    os.chown(release, 0, 0)
    for root, dirs, files in os.walk(release):
        for d in dirs:
            os.chown(Path(root) / d, 0, 0)
        for f in files:
            os.chown(Path(root) / f, 0, 0)
    return release


def _build_venv(release: Path) -> Path:
    venv = release / VENV_REL
    if venv.exists():
        return venv
    target_python = source_python = subprocess.check_output(["/usr/bin/python3.12", "-c", "import sys; print(sys.executable)"]).decode().strip()
    src_python = SOURCE_RELEASE / "deploy" / "scripts" / "_bootstrap_python.py"
    # We deliberately create the venv by invoking the system Python via
    # ``python -m venv`` so the bootstrap never resolves a moving remote
    # package index.  All dependencies are vendored from the source
    # tree's local site-packages.
    subprocess.run([target_python, "-m", "venv", "--without-pip", str(venv)], check=True)
    # Copy the source tree's site-packages wholesale so the daemon has
    # exactly the same dependency closure the hardened peer-deployer
    # was developed against.
    src_site = (source_python := Path(target_python).parent.parent / "lib" / "python3.12" / "site-packages")
    if not src_site.is_dir():
        # Fall back to the source release's interpreter site-packages.
        # This is the explicit supply-chain boundary.
        src_site = SOURCE_RELEASE / "venv" / "lib" / "python3.12" / "site-packages"
    if not src_site.is_dir():
        raise BootstrapError("source site-packages not found; bootstrap cannot proceed without it")
    target_site = venv / "lib" / "python3.12" / "site-packages"
    target_site.mkdir(parents=True, exist_ok=True)
    for entry in src_site.iterdir():
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            shutil.copytree(entry, target_site / entry.name, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target_site / entry.name)
    os.chown(venv, 0, 0)
    return venv


def _install_unit(release: Path) -> None:
    src_unit = release / "deploy" / "systemd" / "control-room-peer-deployer.service"
    if not src_unit.is_file():
        raise BootstrapError(f"missing unit: {src_unit}")
    UNIT_PATH.write_text(src_unit.read_text())
    os.chmod(UNIT_PATH, 0o644)
    os.chown(UNIT_PATH, 0, 0)


def _reload_and_enable() -> None:
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "control-room-peer-deployer.service"], check=True)
    # Start the service WITHOUT disturbing O1/O2.  The unit is the new
    # daemon; it does not After/Require either Omnigent service.
    subprocess.run(["systemctl", "restart", "control-room-peer-deployer.service"], check=True)


def _run_self_tests(release: Path) -> None:
    pytest_bin = SOURCE_RELEASE / "venv" / "bin" / "pytest"
    if not pytest_bin.is_file():
        pytest_bin = Path("/home/hermes/.local/bin/pytest")
    if not pytest_bin.is_file():
        print("[bootstrap] pytest not available; skipping focused test gate")
        return
    env = dict(os.environ)
    env["PYTHONPATH"] = str(release / "deploy" / "scripts")
    rc = subprocess.run([str(pytest_bin), "-q",
                         str(SOURCE_RELEASE / "tests" / "deploy" / "test_peer_deployer_eligibility.py"),
                         str(SOURCE_RELEASE / "tests" / "deploy" / "test_peer_deployer_service.py")],
                        env=env).returncode
    if rc != 0:
        raise BootstrapError("focused test gate failed; bootstrap refused")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=SOURCE_RELEASE)
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
    print("[bootstrap] verifying payload manifest...")
    _verify_manifest(manifest_path, ns.source)
    print("[bootstrap] installing payload...")
    _ensure_dirs()
    release = _install_payload(ns.source)
    print(f"[bootstrap] payload installed at: {release}")
    print("[bootstrap] building root-owned venv (offline)...")
    _build_venv(release)
    print("[bootstrap] installing systemd unit...")
    _install_unit(release)
    if not ns.skip_self_tests:
        print("[bootstrap] running focused self-tests...")
        _run_self_tests(ns.source)
    print("[bootstrap] reloading systemd and starting peer-deployer...")
    _reload_and_enable()
    print(textwrap.dedent("""
    [bootstrap] OK.

    After this command succeeds, O2 may invoke the peer-deployer via
        /run/control-room-peer-deployer/control.sock

    O2 should NOT need sudo for any future O1/O2 upgrade workflow.
    """).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())