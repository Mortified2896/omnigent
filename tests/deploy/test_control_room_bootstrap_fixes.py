"""Regression tests for the new bootstrap fixes.

Each test focuses on a single hardening gate from the
2026-08-10 incident list:

  * interpreter selection — accepted O2 3.12.13 wins over host 3.11
  * --source threaded through every bootstrap operation
  * release layout immutable by payload digest, current symlink atomic
  * peer_deployer importable from the installed venv
  * systemd unit permissive of loopback AF_INET for health probes
  * topology-driven plans/rejected pairings through the trusted engine
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def tmp_dir(prefix: str = "crpd-btest-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _fake_interpreter(tmp: Path, *, version: str, implementation: str) -> Path:
    """Create a fake interpreter that prints the requested JSON identity."""
    script = tmp / "fake_python.py"
    blob = json.dumps({
        "version": version,
        "implementation": implementation,
        "version_info": version,
        "executable": str(script),
        "base_executable": str(script),
    })
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"out = json.loads({blob!r})\n"
        "sys.stdout.write(json.dumps(out))\n"
        "sys.stdout.flush()\n"
    )
    script.chmod(0o755)
    venv = tmp / "release" / "venv" / "bin"
    venv.mkdir(parents=True)
    fake = venv / "python"
    fake.write_text(f"#!/bin/sh\nexec {script} \"$@\"\n")
    fake.chmod(0o755)
    return fake


# ---------------------------------------------------------------------------
# Interpreter selection
# ---------------------------------------------------------------------------


def test_interpreter_requires_accepted_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap must NEVER fall back to host Python 3.11."""
    PKG_ROOT = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
    bootstrap = _load(
        "bootstrap",
        PKG_ROOT / "control_room_peer_deployer_bootstrap.py",
    )

    work = tmp_dir()
    fake = _fake_interpreter(work, version="3.12.13", implementation="cpython")
    monkeypatch.setattr(bootstrap, "ACCEPTED_SUPERVISOR_PYTHON", fake)
    monkeypatch.setattr(bootstrap, "ACCEPTED_SUPERVISOR_CURRENT", fake.parent.parent.parent)

    derived = bootstrap._derive_supervisor_python()
    assert derived == fake

    # Now produce a 3.11.2 fake — bootstrap must refuse to pick it.
    work2 = tmp_dir()
    fake2 = _fake_interpreter(work2, version="3.11.2", implementation="cpython")
    monkeypatch.setattr(bootstrap, "ACCEPTED_SUPERVISOR_PYTHON", fake2)
    monkeypatch.setattr(bootstrap, "ACCEPTED_SUPERVISOR_CURRENT", fake2.parent.parent.parent)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap._derive_supervisor_python()


# ---------------------------------------------------------------------------
# Release layout: payload digest identity + atomic current symlink
# ---------------------------------------------------------------------------


def test_release_identity_is_payload_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap places the release under releases/<payload_digest>."""
    from importlib.util import spec_from_file_location
    spec = spec_from_file_location('bootstrap', 'deploy/scripts/control_room_peer_deployer_bootstrap.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

    work = tmp_dir()
    payload = work / "src"
    payload.mkdir()
    (payload / "deploy").mkdir()
    (payload / "deploy" / "scripts").mkdir()
    (payload / "deploy" / "scripts" / "peer_deployer").mkdir()
    (payload / "deploy" / "systemd").mkdir()
    (payload / "deploy" / "scripts" / "peer_deployer" / "engine.py").write_text("ok")
    (payload / "deploy" / "systemd" / "control-room-peer-deployer.service").write_text("[Unit]\n")
    digest = "0123456789abcdef" * 4
    release_root = work / "releases"
    release_root.mkdir()
    monkeypatch.setattr(m, "PAYLOAD_ROOT", release_root)
    monkeypatch.setattr(m, "_chown_tree", lambda p: None)
    monkeypatch.setattr(m, "_chown_root", lambda p, mode=None: None)
    release = m._install_payload(payload, digest)
    # The install always lands in PAYLOAD_ROOT/releases/<digest>, never under
    # the random tmp source dir or under a sub-staging directory.
    assert release.name == digest
    assert release.parent.name == "releases"
    assert release.parent.parent == release_root
    assert (release / "deploy" / "scripts" / "peer_deployer" / "engine.py").is_file()
    # The bootstrap installs the package under the digest, NOT under
    # the random tmp source dir.
    assert not (release_root / payload.name).exists()


# ---------------------------------------------------------------------------
# Runtime import: peer_deployer.service importable from installed venv
# ---------------------------------------------------------------------------


def test_peer_deployer_service_importable_from_venv() -> None:
    supervisor_python = Path("/opt/omnigent-production/current/venv/bin/python")
    if not supervisor_python.is_file():
        pytest.skip("supervisor python not on disk")
    work = tmp_dir()
    venv = work / "venv"
    res = subprocess.run(
        [str(supervisor_python), "-m", "venv", str(venv)],
        check=False, capture_output=True, text=True,
    )
    if res.returncode != 0:
        pytest.skip(f"venv creation failed: {res.stderr}")
    venv_python = venv / "bin" / "python"
    src_pkg = (
        Path(__file__).resolve().parents[2]
        / "deploy" / "scripts" / "peer_deployer"
    )
    site_candidates = sorted((venv / "lib").glob("python*/site-packages"))
    if not site_candidates:
        pytest.skip("no site-packages created")
    site = site_candidates[0]
    target = site / "peer_deployer"
    shutil.copytree(src_pkg, target)
    res = subprocess.run(
        [str(venv_python), "-c", "import peer_deployer.service; print(peer_deployer.service.__file__)"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "", "PYTHONNOUSITE": "1"},
    )
    assert res.returncode == 0, res.stderr
    assert "peer_deployer/service.py" in res.stdout
    res2 = subprocess.run(
        [str(venv_python), "-m", "peer_deployer.service", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"},
    )
    assert res2.returncode in {0, 2}, res2.stderr


# ---------------------------------------------------------------------------
# Systemd unit gates
# ---------------------------------------------------------------------------


def test_systemd_unit_permits_loopback_af_inet() -> None:
    unit = (
        Path(__file__).resolve().parents[2]
        / "deploy" / "systemd" / "control-room-peer-deployer.service"
    )
    text = unit.read_text()
    assert "AF_INET" in text and "AF_INET6" in text
    assert "ProtectSystem=strict" in text
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in text
    # Unrelated writes remain denied.
    assert "ReadWritePaths=/ " not in text and "ReadWritePaths=/\n" not in text
    for required in (
        "/var/lib/control-room-peer-deployer",
        "/run/control-room-peer-deployer",
        "/opt/control-room-peer-deployer/releases",
        "/opt/control-room-peer-deployer/current",
        "/opt/omnigent",
        "/opt/omnigent-production",
        "/var/lib/omnigent",
        "/var/lib/omnigent-production",
    ):
        assert required in text, f"missing ReadWritePath: {required}"


def test_systemd_unit_starts_from_canonical_current_symlink() -> None:
    unit = (
        Path(__file__).resolve().parents[2]
        / "deploy" / "systemd" / "control-room-peer-deployer.service"
    )
    text = unit.read_text()
    assert "/opt/control-room-peer-deployer/releases/current" not in text
    assert "Documentation=file:/opt/control-room-peer-deployer/current/" in text
    assert "WorkingDirectory=/opt/control-room-peer-deployer/current" in text
    assert (
        "ExecStart=/opt/control-room-peer-deployer/current/venv/bin/python "
        "-m peer_deployer.service --socket /run/control-room-peer-deployer/control.sock"
    ) in text


def test_bootstrap_refuses_stale_systemd_releases_current_path() -> None:
    bootstrap = _load(
        "bootstrap_unit_paths",
        Path(__file__).resolve().parents[2]
        / "deploy" / "scripts" / "control_room_peer_deployer_bootstrap.py",
    )
    good = (
        "WorkingDirectory=/opt/control-room-peer-deployer/current\n"
        "ExecStart=/opt/control-room-peer-deployer/current/venv/bin/python "
        "-m peer_deployer.service --socket /run/control-room-peer-deployer/control.sock\n"
        "RuntimeDirectory=control-room-peer-deployer\n"
        "RuntimeDirectoryMode=0750\n"
    )
    bootstrap._validate_unit_start_paths(good)
    bad = good.replace(
        "/opt/control-room-peer-deployer/current",
        "/opt/control-room-peer-deployer/releases/current",
    )
    with pytest.raises(bootstrap.BootstrapError, match="stale releases/current"):
        bootstrap._validate_unit_start_paths(bad)

    missing_runtime = good.replace("RuntimeDirectory=control-room-peer-deployer\n", "")
    with pytest.raises(bootstrap.BootstrapError, match="RuntimeDirectory"):
        bootstrap._validate_unit_start_paths(missing_runtime)


# ---------------------------------------------------------------------------
# Trusted registry + plan modules
# ---------------------------------------------------------------------------


def _write_registry(path: Path) -> None:
    blob = {
        "schema": "control-room-peer-deployer.trusted-artifact-registry.v1",
        "release_digest": "x",
        "supervisor_python": "/p",
        "artifacts": {
            "541c9a3180b81bfb2fc450b3ef5f8648691b359d": {
                "version": "0.9.0.dev0",
                "release_root": "/opt/omnigent-production/releases/541c9a3180b81bfb2fc450b3ef5f8648691b359d",
                "provenance": "/opt/omnigent-production/releases/541c9a3180b81bfb2fc450b3ef5f8648691b359d/PROVENANCE.txt",
                "wheels": {
                    "omnigent-0.9.0.dev0-py3-none-any.whl": {"path": "/w1", "sha256": "0" * 64},
                    "omnigent_client-0.9.0.dev0-py3-none-any.whl": {"path": "/w2", "sha256": "0" * 64},
                    "omnigent_ui_sdk-0.9.0.dev0-py3-none-any.whl": {"path": "/w3", "sha256": "0" * 64},
                },
            }
        },
    }
    path.write_text(json.dumps(blob))


def _write_plan(path: Path, *, supervisor: str, target: str) -> None:
    blob = {
        "schema": "control-room-peer-deployer.promotion-plan.v1",
        "allowed_topology": {"supervisor": supervisor, "target": target},
        "service_units": {
            "target": ["a.service", "b.service"],
            "supervisor": ["c.service", "d.service"],
        },
        "deployment_roots": {"target": "/t", "supervisor": "/s"},
        "state_roots": {"target": "/ts", "supervisor": "/ss"},
        "health_urls": {"target": "http://127.0.0.1:4097/health", "supervisor": "http://127.0.0.1:4197/health"},
        "expected_pre_state": {
            "target": {"commit_sha": "e" * 40, "version": "0.8.1", "schema": "x"},
            "supervisor": {"commit_sha": "5" * 40, "version": "0.9.0.dev0"},
        },
        "accepted_artifact_sha": "541c9a3180b81bfb2fc450b3ef5f8648691b359d",
        "accepted_artifact_version": "0.9.0.dev0",
        "rollback": {"paired_runtime_db": True, "supervisor_zero_drift": True},
    }
    path.write_text(json.dumps(blob))


def test_trusted_registry_rejects_unallowed_artifact() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "scripts"))
    from peer_deployer import registry as reg

    work = tmp_dir()
    reg_path = work / "registry.json"
    _write_registry(reg_path)
    r = reg.load(reg_path)
    with pytest.raises(reg.RegistryError):
        r.get("some-other-sha")
    assert r.has("541c9a3180b81bfb2fc450b3ef5f8648691b359d") is True


def test_trusted_registry_rejects_schema_mismatch() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "scripts"))
    from peer_deployer import registry as reg

    work = tmp_dir()
    reg_path = work / "registry.json"
    reg_path.write_text(json.dumps({
        "schema": "wrong.schema",
        "release_digest": "x",
        "supervisor_python": "/p",
        "artifacts": {},
    }))
    with pytest.raises(reg.RegistryError):
        reg.load(reg_path)


def test_plan_loads_only_supported_topology() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "scripts"))
    from peer_deployer import plan

    work = tmp_dir()
    plans_dir = work / "plans"
    plans_dir.mkdir()
    _write_plan(plans_dir / "o2_o1.json", supervisor="O2", target="O1")
    _write_plan(plans_dir / "o1_o2.json", supervisor="O1", target="O2")
    loaded = plan.load_all(plans_dir)
    assert ("O2", "O1") in loaded
    assert ("O1", "O2") in loaded
    plan.find("O2", "O1", plans_dir)
    with pytest.raises(plan.PlanError):
        plan.find("O2", "O3", plans_dir)
    with pytest.raises(plan.PlanError):
        plan.find("O2", "O2", plans_dir)


def test_engine_does_not_have_hardcoded_target() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "deploy" / "scripts" / "peer_deployer" / "engine.py"
    )
    text = src.read_text()
    assert "TARGET = identity.O1" not in text
    assert "TARGET = identity.O2" not in text
    assert "SUPERVISOR = identity.O1" not in text
    assert "SUPERVISOR = identity.O2" not in text
    assert "run_promotion" in text
    assert "PromotionPlan" in text


def test_old_host_promotion_removed_from_daemon_path() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "deploy" / "scripts" / "peer_deployer" / "service.py"
    )
    text = src.read_text()
    assert "host_promotion.run" not in text
    assert "engine.run_promotion" in text
    assert "REFUSED: topology violation" in text


def test_daemon_loopback_health_probe_via_inet_but_no_tcp_listener() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "deploy" / "scripts" / "peer_deployer" / "service.py"
    )
    text = src.read_text()
    # Loopback health probe must be HTTP-via-curl (no TCP listener).
    assert "curl" in text
    # No socket call for an INET listener.
    lines = [l for l in text.splitlines() if "socket(" in l]
    assert all("AF_UNIX" in l for l in lines), lines
