"""Focused tests for the permanent root-owned peer-deployer service.

These tests exercise the narrow request protocol, the caller's cgroup
binding, the O1/O2 distinct-target invariant, and the exclusive
deployment lock — without touching the live O1/O2 runtimes.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from peer_deployer import eligibility, service, transaction
from peer_deployer.eligibility import transaction as eligibility_transaction


# --- Stubs for the trusted registry / plans so tests do not need the real files.


@pytest.fixture(autouse=True)
def _socket_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(service, "SOCKET_PATH", tmp_path / "control.sock")
    monkeypatch.setattr(service, "MAX_REQUEST_BYTES", 4096)
    monkeypatch.setattr(service, "CONTROL_ROOT", tmp_path)
    monkeypatch.setattr(service, "RUN_ROOT", tmp_path)
    monkeypatch.setattr(service, "LOCK_PATH", tmp_path / "locks" / "deployment.lock")
    monkeypatch.setattr(service, "EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(service, "REGISTRY_PATH", tmp_path / "artifacts" / "registry.json")
    monkeypatch.setattr(service, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(service, "TX_ROOT", tmp_path / "transactions")
    monkeypatch.setattr(service, "TRANSACTION_ROOT", tmp_path / "transactions")
    monkeypatch.setattr(eligibility_transaction, "DEFAULT_TX_ROOT", tmp_path / "transactions")
    monkeypatch.setattr(transaction, "DEFAULT_TX_ROOT", tmp_path / "transactions")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "plans").mkdir()
    registry = {
        "schema": "control-room-peer-deployer.trusted-artifact-registry.v1",
        "release_digest": "x" * 64,
        "supervisor_python": "/opt/omnigent-production/current/venv/bin/python",
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
    (tmp_path / "artifacts" / "registry.json").write_text(json.dumps(registry))
    plan = {
        "schema": "control-room-peer-deployer.promotion-plan.v1",
        "allowed_topology": {"supervisor": "O2", "target": "O1"},
        "service_units": {
            "target": ["omnigent.service", "omnigent-host.service"],
            "supervisor": ["omnigent-production.service", "omnigent-production-host.service"],
        },
        "deployment_roots": {"target": "/opt/omnigent", "supervisor": "/opt/omnigent-production"},
        "state_roots": {"target": "/var/lib/omnigent", "supervisor": "/var/lib/omnigent-production"},
        "health_urls": {"target": "http://127.0.0.1:4097/health", "supervisor": "http://127.0.0.1:4197/health"},
        "expected_pre_state": {
            "target": {"commit_sha": "e" * 40, "version": "0.8.1", "schema": "x"},
            "supervisor": {"commit_sha": "5" * 40, "version": "0.9.0.dev0"},
        },
        "accepted_artifact_sha": "541c9a3180b81bfb2fc450b3ef5f8648691b359d",
        "accepted_artifact_version": "0.9.0.dev0",
        "rollback": {"paired_runtime_db": True, "supervisor_zero_drift": True},
    }
    (tmp_path / "plans" / "o2_supervises_o1.json").write_text(json.dumps(plan))
    return tmp_path


def _send(payload: dict, *, cgroup_unit: str = "omnigent-production.service", uid: int = 1000) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(service.SOCKET_PATH))
    client.sendall((json.dumps(payload) + "\n").encode())
    line = client.makefile("r").readline()
    client.close()
    return json.loads(line)


@pytest.fixture
def fake_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.Handler, "setup", lambda self: None)


def test_status_opening_requires_no_creds(_socket_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = service.DeploymentManager(dry_run=True)
    out = manager.status()
    assert out["ok"] is True
    assert out["service"] == "control-room-peer-deployer"


def test_unknown_op_rejected(_socket_dir: Path) -> None:
    with pytest.raises(service.ProtocolError):
        service.validate_request({"op": "rm", "target": "/etc/passwd"})


def test_request_with_arbitrary_keys_rejected(_socket_dir: Path) -> None:
    with pytest.raises(service.ProtocolError):
        service.validate_request({"op": "status", "exec": "rm -rf /"})
    with pytest.raises(service.ProtocolError):
        service.validate_request({"op": "promote", "target": "O1", "request_id": "ok", "command": "rm"})


def test_installer_health_root_bypasses_omnigent_auth(_socket_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UID 0 must reach installer_health without entering O1/O2 auth.

    This is the exact regression that caused the live bootstrap to report
    ``caller uid is not the Omnigent service uid`` even though the handler
    intended installer_health to be root-only.
    """
    manager = service.DeploymentManager(dry_run=True)

    class FakeRequest:
        def getsockopt(self, *_args: object) -> bytes:
            return (
                (4321).to_bytes(4, "little")
                + (0).to_bytes(4, "little")
                + (0).to_bytes(4, "little")
            )

    handler = service.Handler.__new__(service.Handler)
    handler.rfile = io.BytesIO(b'{"op":"installer_health"}\n')
    handler.wfile = io.BytesIO()
    handler.request = FakeRequest()
    handler.manager = manager

    def _must_not_authenticate(_pid: int, _uid: int) -> str:
        raise AssertionError("installer_health entered Omnigent authentication")

    monkeypatch.setattr(service, "authenticated_instance", _must_not_authenticate)
    handler.handle()
    payload = json.loads(handler.wfile.getvalue().decode())

    assert payload["ok"] is True
    assert payload["scope"] == "installer_health"
    assert payload["registry_loadable"] is True


def test_installer_health_root_only(_socket_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer_health op is restricted to UID 0.

    The bootstrap installer runs as root; the application UIDs (hermes)
    must NOT be able to use this op to bypass authentication.
    """
    blob = service.validate_request({"op": "installer_health"})
    assert blob["op"] == "installer_health"

    import socket as _socket
    sock = service.SOCKET_PATH
    sock.parent.mkdir(parents=True, exist_ok=True)
    server_thread = threading.Thread(
        target=service.serve, args=(sock,), daemon=True,
    )
    server_thread.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and not sock.exists():
            time.sleep(0.05)
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(sock))
            client.sendall(b'{"op":"installer_health"}\n')
            reply = client.recv(65536).decode(errors="replace")
        payload = json.loads(reply)
        assert payload.get("ok") is False, payload
        assert "AuthorizationError" in payload.get("error", ""), payload
        assert "root-only" in payload.get("error", ""), payload
    finally:
        import os as _os
        try:
            _os.unlink(sock)
        except FileNotFoundError:
            pass
        server_thread.join(timeout=1)


def test_self_upgrade_refused_by_caller_cgroup() -> None:
    from peer_deployer import identity
    target = identity.O2; supervisor = identity.O2
    with pytest.raises(identity.IdentityError):
        identity.require_distinct(target, supervisor)


def test_only_recognized_cgroups_map_to_o1_or_o2() -> None:
    monkey_cgroup = "unknown.service\n"
    with patch("pathlib.Path.read_text", return_value=monkey_cgroup):
        with pytest.raises(service.AuthorizationError):
            service.authenticated_instance(1, 1000)


def test_only_recognized_omngent_cgroup_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_pid_cgroup_unit", lambda pid: "omnigent.service")
    monkeypatch.setattr(service.pwd, "getpwnam", lambda name: type("pw", (), {"pw_uid": 1000})())
    assert service.authenticated_instance(1, 1000) == "O1"
    monkeypatch.setattr(service, "_pid_cgroup_unit", lambda pid: "omnigent-production.service")
    assert service.authenticated_instance(1, 1000) == "O2"
    with pytest.raises(service.AuthorizationError):
        service.authenticated_instance(1, 2000)
    monkeypatch.setattr(service, "_pid_cgroup_unit", lambda pid: "bogus.service")
    with pytest.raises(service.AuthorizationError):
        service.authenticated_instance(1, 1000)


def test_blocking_preflight_blocks_promotion(_socket_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tx_root = _socket_dir / "transactions"
    (tx_root).mkdir()
    tx_id = "promotion-20260101T000000Z-feedfeed"
    (tx_root / tx_id).mkdir()
    (tx_root / tx_id / "transaction.json").write_text(json.dumps({"phase": "candidate_staging"}))
    monkeypatch.setattr(service, "TRANSACTION_ROOT", tx_root)
    monkeypatch.setattr(service, "TX_ROOT", tx_root)
    monkeypatch.setattr(eligibility_transaction, "DEFAULT_TX_ROOT", tx_root)
    monkeypatch.setattr(transaction, "DEFAULT_TX_ROOT", tx_root)
    manager = service.DeploymentManager(dry_run=True)
    plan = manager.trusted.plan_for("O2", "O1")
    with pytest.raises(transaction.TransactionError):
        manager.submit(plan=plan, promote=True)


def test_valid_reconciliation_overlay_allows_promotion(_socket_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tx_root = _socket_dir / "transactions"
    (tx_root).mkdir()
    tx_id = "promotion-20260101T000000Z-deadbeef"
    (tx_root / tx_id).mkdir()
    (tx_root / tx_id / "transaction.json").write_text(json.dumps({"phase": "candidate_staging"}))
    (tx_root / tx_id / "reconciliation.json").write_text(json.dumps({
        "tx_id": tx_id,
        "phase": "candidate_staging",
        "classification": eligibility.CLASS_VALIDLY_RECONCILED,
        "blocks": False,
        "reason": "staging cleaned by host_crash recovery",
    }))
    monkeypatch.setattr(service, "TRANSACTION_ROOT", tx_root)
    monkeypatch.setattr(service, "TX_ROOT", tx_root)
    monkeypatch.setattr(eligibility_transaction, "DEFAULT_TX_ROOT", tx_root)
    monkeypatch.setattr(transaction, "DEFAULT_TX_ROOT", tx_root)
    manager = service.DeploymentManager(dry_run=True)
    plan = manager.trusted.plan_for("O2", "O1")
    status = manager.submit(plan=plan, promote=True)
    assert status.tx_id != tx_id
