"""Failure injection at actual journal/symlink/SQLite mutation boundaries."""

import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/scripts"))
from peer_deployer import rtx
from peer_deployer.rtx_contract import (
    Journal,
    Peer,
    Refused,
    database_evidence,
    digest,
    distinct,
)

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    peers = []
    for index in [1, 2]:
        peer = Peer(
            f"O{index}",
            tmp_path / f"o{index}",
            4097 + (index - 1) * 100,
            1111 * index,
            str(index) * 32,
            str(index + 2) * 32,
        )
        (peer.root / "state").mkdir(parents=True)
        with sqlite3.connect(peer.db) as c:
            c.execute("create table rtx_instance_identity(instance, identity)")
            c.execute(
                "insert into rtx_instance_identity values (?,?)", (peer.instance, peer.database_id)
            )
            c.execute("create table alembic_version(version_num)")
            c.execute("insert into alembic_version values ('schema')")
            for table in ["conversations", "conversation_items", "response_feedback"]:
                c.execute(f"create table {table}(value)")
                c.execute(f"insert into {table} values ('retained')")
        old = peer.root / "releases" / OLD
        old.mkdir(parents=True)
        peer.current.symlink_to(old)
        peers.append(peer)
    target, supervisor = peers
    new = tmp_path / "releases" / NEW
    new.mkdir(parents=True)
    artifact = {"source_sha": NEW, "runtime": str(new), "schema": "schema"}
    monkeypatch.setattr(rtx, "TRANSACTIONS", tmp_path / "transactions")
    real_run = rtx.run
    monkeypatch.setattr(
        rtx, "run", lambda args, **kwargs: "" if args[0] == "chown" else real_run(args, **kwargs)
    )
    monkeypatch.setattr(rtx, "accepted", lambda *_: artifact)
    monkeypatch.setattr(rtx, "info", lambda _: {"build_sha": OLD})

    def snapshot(peer, sha):
        actual = peer.current.resolve().name
        if actual != sha:
            raise Refused("wrong expected-current SHA")
        return {"database": database_evidence(peer), "sha": sha}

    monkeypatch.setattr(rtx, "snapshot", snapshot)
    events = []
    monkeypatch.setattr(rtx, "stop", lambda peer: events.append(("stop", peer.instance)))

    def start(peer, sha):
        events.append(("start", peer.instance, sha))
        return snapshot(peer, sha)

    monkeypatch.setattr(rtx, "start", start)
    return target, supervisor, events, artifact


def promote(fixture, **kwargs):
    target, supervisor, _, _ = fixture
    return rtx.promote(
        target,
        supervisor,
        kwargs.get("expected", OLD),
        Path("unused"),
        "c" * 64,
        kwargs.get("tx_id", "test-transaction-001"),
    )


def test_self_upgrade_refused(fixture):
    target, _, events, _ = fixture
    with pytest.raises(Refused, match="collide"):
        rtx.promote(target, target, OLD, Path("unused"), "c" * 64, "test-self-001")
    assert events == []


@pytest.mark.parametrize("field", ["host_id", "database_id", "port", "root", "public_port"])
def test_colliding_identities_refused(fixture, field):
    target, supervisor, _, _ = fixture
    with pytest.raises(Refused, match="collide"):
        distinct(target, replace(supervisor, **{field: getattr(target, field)}))


def test_wrong_current_is_zero_mutation(fixture):
    target, _, events, _ = fixture
    original = digest(target.db), os.readlink(target.current)
    with pytest.raises(Refused, match="expected-current"):
        promote(fixture, expected="d" * 40)
    assert (digest(target.db), os.readlink(target.current)) == original
    assert events == []


@pytest.mark.parametrize("failure", ["artifact", "supervisor", "schema"])
def test_preflight_failures_do_not_touch_active(fixture, monkeypatch, failure):
    target, supervisor, events, artifact = fixture
    original = digest(target.db), os.readlink(target.current)
    if failure == "artifact":

        def refuse(*_):
            raise Refused("artifact mismatch")

        monkeypatch.setattr(rtx, "accepted", refuse)
    elif failure == "supervisor":
        monkeypatch.setattr(rtx, "info", lambda _: {})
    else:
        artifact["schema"] = "wrong-schema"
    with pytest.raises(Refused):
        promote(fixture)
    assert (digest(target.db), os.readlink(target.current)) == original
    assert events == []
    assert supervisor.current.resolve().name == OLD


def test_success_records_boundary_and_preserves_supervisor(fixture):
    target, supervisor, events, _ = fixture
    original = digest(supervisor.db), os.readlink(supervisor.current)
    result = promote(fixture)
    assert result["status"] == "committed"
    assert result["mutation_boundary"] is True
    assert target.current.resolve().name == NEW
    assert (digest(supervisor.db), os.readlink(supervisor.current)) == original
    assert all(event[1] == "O1" for event in events)
    assert Path(result["old_release"]).is_dir()
    assert Path(result["backup"]["path"]).is_file()


def test_broken_start_restores_db_and_release(fixture, monkeypatch):
    target, supervisor, _events, _ = fixture
    real_start = rtx.start

    def broken(peer, sha):
        if sha == NEW:
            with sqlite3.connect(peer.db) as c:
                c.execute("delete from response_feedback")
            raise Refused("broken candidate")
        return real_start(peer, sha)

    monkeypatch.setattr(rtx, "start", broken)
    with pytest.raises(Refused, match="broken candidate"):
        promote(fixture)
    assert target.current.resolve().name == OLD
    assert database_evidence(target)["counts"]["response_feedback"] == 1
    records = list(rtx.TRANSACTIONS.glob("*/transaction.json"))
    record = json.loads(records[0].read_text())
    assert record["status"] == "rolled_back"
    assert Path(record["backup"]["path"]).exists()
    assert supervisor.current.resolve().name == OLD


def test_unowned_and_preboundary_rollback_refused(fixture, tmp_path):
    target, supervisor, _, _ = fixture
    tx = Journal.create(
        tmp_path / "tx.json",
        target=target,
        supervisor=supervisor,
        expected=OLD,
        old="old",
        accepted="new",
        artifact_digest="c" * 64,
    )
    with pytest.raises(Refused, match="before mutation"):
        tx.authorize_rollback([str(target.db)])
    tx.save(mutation_boundary=True)
    with pytest.raises(Refused, match="unowned"):
        tx.authorize_rollback([str(supervisor.db)])
    with pytest.raises(Refused, match="cannot be cleared"):
        tx.save(mutation_boundary=False)


def test_replay_refused(fixture):
    promote(fixture)
    with pytest.raises(FileExistsError):
        promote(fixture, expected=NEW)


def test_stale_transaction_blocks(fixture):
    root = rtx.TRANSACTIONS / "stale-transaction"
    root.mkdir(parents=True)
    (root / "transaction.json").write_text('{"status": "starting"}')
    with pytest.raises(Refused, match="unresolved transaction"):
        promote(fixture)


def test_live_lock_blocks(fixture):
    import fcntl

    rtx.TRANSACTIONS.mkdir()
    with (rtx.TRANSACTIONS / "deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Refused, match="in flight"):
            promote(fixture)


def test_wrong_or_fresh_database_refused(fixture):
    target, _, _, _ = fixture
    with sqlite3.connect(target.db) as c:
        c.execute("update rtx_instance_identity set identity='wrong'")
    with pytest.raises(Refused, match="DB binding"):
        database_evidence(target)
    target.db.unlink()
    with pytest.raises(Refused, match="missing"):
        database_evidence(target)
