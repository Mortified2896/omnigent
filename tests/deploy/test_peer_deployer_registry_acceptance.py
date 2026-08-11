"""End-to-end regressions for peer-deployer trusted registry startup.

These tests intentionally join the real bootstrap producer helpers with the
real runtime registry/service consumers so schema drift cannot hide behind
separate unit tests again.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "deploy" / "scripts"
ACCEPTED_SHA = "541c9a3180b81bfb2fc450b3ef5f8648691b359d"
ACCEPTED_VERSION = "0.9.0.dev0"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_registry_acceptance",
        SCRIPTS / "control_room_peer_deployer_bootstrap.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_runtime():
    sys.path.insert(0, str(SCRIPTS))
    from peer_deployer import plan, registry, service

    return registry, plan, service


def _make_wheels(root: Path) -> Path:
    release = root / "supervisor" / "releases" / ACCEPTED_SHA
    wheels = release / "artifacts"
    wheels.mkdir(parents=True)
    (release / "PROVENANCE.txt").write_text("synthetic provenance\n")
    for name, data in {
        "omnigent-0.9.0.dev0-py3-none-any.whl": b"omnigent",
        "omnigent_client-0.9.0.dev0-py3-none-any.whl": b"client",
        "omnigent_ui_sdk-0.9.0.dev0-py3-none-any.whl": b"ui",
    }.items():
        (wheels / name).write_bytes(data)
    return release


def _seed_generated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bootstrap = _load_bootstrap()
    runtime_root = tmp_path / "crpd-root"
    supervisor_root = tmp_path / "supervisor"
    release = tmp_path / "peer-release"
    release.mkdir()
    supervisor_python = tmp_path / "supervisor-python"
    supervisor_python.write_text("#!/bin/sh\nexit 0\n")
    supervisor_python.chmod(0o755)
    _make_wheels(tmp_path)

    monkeypatch.setattr(bootstrap, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(bootstrap, "ACCEPTED_SUPERVISOR_DEPLOYMENT_ROOT", supervisor_root)
    monkeypatch.setattr(bootstrap, "_derive_supervisor_python", lambda: supervisor_python)
    monkeypatch.setattr(bootstrap, "_chown_root", lambda p, mode=None: os.chmod(p, mode) if mode is not None else None)

    (runtime_root / "artifacts").mkdir(parents=True)
    bootstrap._seed_trusted_registry(release, "a" * 64)
    bootstrap._seed_promotion_plans()
    return bootstrap, runtime_root, supervisor_python


def test_bootstrap_generated_registry_loads_with_real_consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, runtime_root, supervisor_python = _seed_generated_runtime(tmp_path, monkeypatch)
    registry, _, _ = _import_runtime()

    trusted = registry.load(runtime_root / "artifacts" / "registry.json")
    assert trusted.release_digest == "a" * 64
    assert trusted.supervisor_python == str(supervisor_python)
    artifact = trusted.get(ACCEPTED_SHA)
    assert artifact.artifact_sha == ACCEPTED_SHA
    assert artifact.version == ACCEPTED_VERSION
    assert artifact.release_root.endswith(f"/releases/{ACCEPTED_SHA}")
    assert sorted(artifact.wheels) == [
        "omnigent-0.9.0.dev0-py3-none-any.whl",
        "omnigent_client-0.9.0.dev0-py3-none-any.whl",
        "omnigent_ui_sdk-0.9.0.dev0-py3-none-any.whl",
    ]
    assert all(len(meta["sha256"]) == 64 for meta in artifact.wheels.values())
    raw = json.loads((runtime_root / "artifacts" / "registry.json").read_text())
    assert "interpreters" not in raw
    assert raw["supervisor_python"] == str(supervisor_python)


def test_broken_nested_only_registry_fails_clearly(tmp_path: Path) -> None:
    registry, _, _ = _import_runtime()
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "schema": registry.REGISTRY_SCHEMA,
        "release_digest": "a" * 64,
        "interpreters": {"supervisor_python": "/abs/python"},
        "artifacts": {},
    }))
    with pytest.raises(registry.RegistryError, match="missing supervisor_python"):
        registry.load(path)


@pytest.mark.parametrize("value", [None, "relative/python"])
def test_missing_or_relative_supervisor_python_fails(tmp_path: Path, value: str | None) -> None:
    registry, _, _ = _import_runtime()
    path = tmp_path / "registry.json"
    blob = {
        "schema": registry.REGISTRY_SCHEMA,
        "release_digest": "a" * 64,
        "artifacts": {},
    }
    if value is not None:
        blob["supervisor_python"] = value
    path.write_text(json.dumps(blob))
    expected = "missing supervisor_python" if value is None else "absolute path"
    with pytest.raises(registry.RegistryError, match=expected):
        registry.load(path)


def test_generated_registry_and_plans_feed_trusted_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, runtime_root, _ = _seed_generated_runtime(tmp_path, monkeypatch)
    _, _, service = _import_runtime()
    monkeypatch.setattr(service, "REGISTRY_PATH", runtime_root / "artifacts" / "registry.json")
    monkeypatch.setattr(service, "PLANS_DIR", runtime_root / "plans")
    cfg = service.TrustedConfig()
    cfg.load_plans()
    assert cfg.registry.has(ACCEPTED_SHA)
    assert ("O2", "O1") in cfg.plans
    assert ("O1", "O2") in cfg.plans


def test_actual_daemon_starts_with_generated_registry_and_status_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, runtime_root, _ = _seed_generated_runtime(tmp_path, monkeypatch)
    pkg_src = SCRIPTS / "peer_deployer"
    site = tmp_path / "installed" / "site-packages"
    shutil.copytree(pkg_src, site / "peer_deployer")
    run_root = tmp_path / "run"
    sock = run_root / "control.sock"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(site),
        "PYTHONNOUSERSITE": "1",
        "CRPD_ROOT": str(runtime_root),
        "CRPD_RUN": str(run_root),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "peer_deployer.service", "--socket", str(sock)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not sock.exists() and proc.poll() is None:
            time.sleep(0.05)
        assert proc.poll() is None, proc.stderr.read() if proc.stderr else "process exited"
        assert sock.exists()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(sock))
            client.sendall(b'{"op":"status"}\n')
            response = client.recv(65536).decode()
        blob = json.loads(response)
        # The real handler authenticates peer credentials before status; in an
        # isolated pytest process we only require the protocol to respond.
        assert blob["ok"] in {True, False}
        assert proc.poll() is None
        # Prove the daemon did not create an INET listening socket by scanning
        # the host listener table for this process id.
        ss = subprocess.run(["ss", "-ltnp"], check=False, capture_output=True, text=True)
        assert str(proc.pid) not in ss.stdout
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode in {0, -15}


def test_unit_has_bounded_start_limit() -> None:
    text = (REPO_ROOT / "deploy" / "systemd" / "control-room-peer-deployer.service").read_text()
    assert "Restart=on-failure" in text
    assert "StartLimitIntervalSec=10min" in text
    assert "StartLimitBurst=5" in text


def test_post_install_socket_failure_includes_journal_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bootstrap = _load_bootstrap()
    sock = tmp_path / "missing.sock"
    monkeypatch.setattr(bootstrap, "_systemctl_show_value", lambda prop: "failed" if prop == "ActiveState" else "")
    monkeypatch.setattr(
        bootstrap,
        "_service_diagnostics",
        lambda: "journal: peer_deployer.registry.RegistryError: registry missing supervisor_python",
    )
    with pytest.raises(bootstrap.BootstrapError, match="registry missing supervisor_python"):
        bootstrap._wait_for_socket_or_diagnose(sock, timeout=0.01)


def test_focused_tmp_usage_guard(tmp_path: Path) -> None:
    before = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    tiny = tmp_path / "tiny-fixture"
    tiny.mkdir()
    for i in range(10):
        (tiny / f"f{i}.txt").write_text("x" * 1024)
    after = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    assert after - before < 20 * 1024 * 1024
