"""Cold maintenance refuses uncertain work and never force-kills it."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.scripts.peer_deployer import baseline, identity, transaction
from deploy.scripts.peer_deployer import cold_maintenance as m


def test_session_inventory_uses_fresh_details_and_archived(monkeypatch):
    seen = []

    def request(base, path):
        seen.append(path)
        if "?" in path:
            return {"data": [{"id": "abc", "status": "running"}], "has_more": False}
        return {"status": "idle", "pending_elicitations_count": 0}

    monkeypatch.setattr(m, "request", request)
    assert m.idle_sessions("http://local")["count"] == 1
    assert "include_archived=true" in seen[0]


@pytest.mark.parametrize(
    "reply",
    [
        {"status": "running"},
        {"status": "unknown"},
        {},
        {"status": "idle", "pending_elicitations_count": 1},
    ],
)
def test_uncertain_or_active_work_refused(monkeypatch, reply):
    monkeypatch.setattr(
        m,
        "request",
        lambda base, path: {"data": [{"id": "abc"}], "has_more": False} if "?" in path else reply,
    )
    with pytest.raises(m.MaintenanceError):
        m.idle_sessions("http://local")


def test_session_probe_error_is_not_idle(monkeypatch):
    def fail(*args):
        raise OSError("disconnected")

    monkeypatch.setattr(m, "request", fail)
    with pytest.raises(OSError):
        m.idle_sessions("http://local")


def test_partial_inventory_refused(monkeypatch):
    monkeypatch.setattr(m, "request", lambda *args: {"data": []})
    with pytest.raises(m.MaintenanceError, match="incomplete_inventory"):
        m.idle_sessions("http://local")


@pytest.fixture
def host(tmp_path, monkeypatch):
    machine = tmp_path / "machine-id"
    machine.write_text("source-machine")
    monkeypatch.setattr(m, "MACHINE_ID_FILE", machine)
    monkeypatch.setattr(m, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(m, "SYSTEMD_UNIT_ROOT", tmp_path / "units")
    (tmp_path / "units").mkdir()
    txroot = tmp_path / "transactions"
    txroot.mkdir()
    monkeypatch.setattr(transaction, "DEFAULT_TX_ROOT", txroot)
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "chat.db") as db:
        db.execute("CREATE TABLE alembic_version(version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('schema')")
        db.execute("CREATE TABLE conversations(id TEXT)")
        db.execute("INSERT INTO conversations VALUES ('saved-work')")
    monkeypatch.setattr(m.preflight, "target_home_for", lambda target: data)

    def snap(i):
        return baseline.SupervisorBaseline(
            i.name,
            "a" * 40,
            "v1",
            baseline.UnitBaseline(i.service_unit, "active", 1, 100),
            baseline.UnitBaseline(i.host_unit, "active", 2, 101),
        )

    monkeypatch.setattr(m.baseline, "capture", snap)
    monkeypatch.setattr(
        m,
        "run_checks",
        lambda t, s, p: (SimpleNamespace(to_dict=lambda: {"passed": True}), snap(t)),
    )
    monkeypatch.setattr(m, "idle_sessions", lambda base: {"count": 0})
    monkeypatch.setattr(m, "process_inventory", lambda target: [])
    monkeypatch.setattr(m.host_promotion, "_no_live_transactions", lambda: None)
    monkeypatch.setattr(
        m.preflight,
        "default_storage_guard_probe",
        lambda: SimpleNamespace(active=True, latched=False),
    )
    accepted = SimpleNamespace(
        source_sha="a" * 40,
        package_version="v1",
        acceptance_record_sha256="b" * 64,
        wheels=[
            SimpleNamespace(role=r, sha256="c" * 64) for r in ("main", "sdk_client", "sdk_ui")
        ],
    )
    monkeypatch.setattr(m.acceptance, "load", lambda *a, **k: accepted)
    monkeypatch.setattr(
        m.identity, "_resolve_active_python", lambda root: tmp_path / "runtime/bin/python"
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "enabled\n", "")

    monkeypatch.setattr(m.subprocess, "run", run)
    monkeypatch.setattr(m, "wait_stopped", lambda unit: None)
    args = {
        "target": identity.O1,
        "supervisor": identity.O2,
        "record_path": tmp_path / "acceptance.json",
        "expected_machine_id": "source-machine",
        "expected_supervisor": snap(identity.O2).to_dict(),
        "apply": True,
    }
    return args, calls, data, txroot


def test_guard_rechecks_before_first_service_mutation(host, monkeypatch):
    args, calls, data, root = host
    before = (data / "chat.db").read_bytes()
    monkeypatch.setattr(
        m.preflight,
        "default_storage_guard_probe",
        lambda: SimpleNamespace(active=True, latched=True),
    )
    with pytest.raises(m.MaintenanceError, match="storage_guard"):
        m.freeze(**args)
    assert all(call[1] == "is-enabled" for call in calls)
    assert (data / "chat.db").read_bytes() == before
    record = json.loads(next(root.glob("*/transaction.json")).read_text())
    assert record["mutation_boundary_crossed"] is False


def test_self_supervision_refused_before_writes(host):
    args, calls, _data, root = host
    args["supervisor"] = identity.O1
    with pytest.raises(identity.IdentityError):
        m.freeze(**args)
    assert not list(root.iterdir()) and not calls


def test_freeze_retains_backup_and_supervisor(host):
    args, calls, data, _root = host
    before = (data / "chat.db").read_bytes()
    result = m.freeze(**args)
    assert result["phase"] == "source-frozen"
    assert (Path(result["transaction"]) / "pre-stop-chat.db").is_file()
    assert (data / "chat.db").read_bytes() == before
    assert ["systemctl", "--no-block", "stop", "omnigent.service"] in calls
    assert ["systemctl", "--no-block", "stop", "omnigent-host.service"] in calls
    assert not any(
        "omnigent-production.service" in c or "omnigent-production-host.service" in c
        for c in calls
    )
    for path in result["dropins"]:
        text = Path(path).read_text()
        assert "SendSIGKILL=no" in text and "TimeoutStopSec=infinity" in text
    assert Path(result["fence"]).is_file()


def test_failed_server_drain_keeps_host_and_backup(host, monkeypatch):
    args, calls, _data, root = host

    def fail(unit):
        raise m.MaintenanceError("still draining")

    monkeypatch.setattr(m, "wait_stopped", fail)
    with pytest.raises(m.MaintenanceError):
        m.freeze(**args)
    assert ["systemctl", "--no-block", "stop", "omnigent-host.service"] not in calls
    assert next(root.glob("*/pre-stop-chat.db")).is_file()
    record = json.loads(next(root.glob("*/transaction.json")).read_text())
    assert record["mutation_boundary_crossed"] is True
    assert record["phase"] != "tx_committed"


def test_repeated_freeze_does_not_mutate_again(host):
    args, calls, _data, root = host
    first = m.freeze(**args)
    count = len(calls)
    second = m.freeze(**args)
    assert second == first and len(calls) == count
    assert len(list(root.iterdir())) == 1
