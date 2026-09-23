"""Regression coverage for the independent RTX deployment controller."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/scripts"))
from peer_deployer import external_rtx, rtx
from peer_deployer.rtx_contract import Peer, Refused, database_evidence, digest

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    target = Peer("O1", tmp_path / "o1", 4097, 1111, "1" * 32, "3" * 32)
    (target.root / "state").mkdir(parents=True)
    with sqlite3.connect(target.db) as connection:
        connection.execute("create table rtx_instance_identity(instance, identity)")
        connection.execute(
            "insert into rtx_instance_identity values (?,?)", (target.instance, target.database_id)
        )
        connection.execute("create table alembic_version(version_num)")
        connection.execute("insert into alembic_version values ('schema')")
        for table in ("conversations", "conversation_items", "response_feedback"):
            connection.execute(f"create table {table}(value)")
            connection.execute(f"insert into {table} values ('retained')")

    old_release = tmp_path / "releases" / OLD
    old_release.mkdir(parents=True)
    target.current.symlink_to(old_release)
    new_release = tmp_path / "releases" / NEW
    new_release.mkdir()
    artifact = {"source_sha": NEW, "runtime": str(new_release), "schema": "schema"}

    transaction_root = tmp_path / "transactions"
    monkeypatch.setattr(rtx, "TRANSACTIONS", transaction_root)
    monkeypatch.setattr(external_rtx, "external_controller_guard", lambda: None)
    monkeypatch.setattr(rtx, "accepted", lambda *_: artifact)
    monkeypatch.setattr(external_rtx, "_health", lambda _: {"status": "ok"})
    monkeypatch.setattr(external_rtx, "_check_headroom", lambda _: None)

    def snapshot(peer, sha):
        if peer.current.resolve().name != sha:
            raise Refused("wrong expected-current SHA")
        return {"database": database_evidence(peer), "sha": sha}

    monkeypatch.setattr(rtx, "snapshot", snapshot)
    events = []
    monkeypatch.setattr(rtx, "stop", lambda peer: events.append(("stop", peer.instance)))

    def start(peer, sha):
        events.append(("start", peer.instance, sha))
        return snapshot(peer, sha)

    monkeypatch.setattr(rtx, "start", start)
    return target, events, artifact, transaction_root


def promote(fixture, *, tx_id="external-models-001", expected=OLD):
    target, _, _, _ = fixture
    return external_rtx.promote(
        target,
        expected,
        Path("unused"),
        "c" * 64,
        tx_id,
    )


def test_external_promotion_has_no_supervisor_and_serializes(fixture):
    target, events, _, transactions = fixture

    result = promote(fixture)

    assert result["status"] == "committed"
    assert result["controller"] == "external"
    assert "supervisor" not in result
    assert target.current.resolve().name == NEW
    assert [event[0] for event in events] == ["stop", "start"]
    assert Path(result["backup"]["path"]).is_file()
    assert Path(result["backup"]["state_archive"]).is_file()
    assert json.loads(next(transactions.glob("*/transaction.json")).read_text())["status"] == (
        "committed"
    )


def test_failed_external_start_restores_target_and_retains_evidence(fixture, monkeypatch):
    target, _, _, transactions = fixture
    start = rtx.start

    def broken_start(peer, sha):
        if sha == NEW:
            with sqlite3.connect(peer.db) as connection:
                connection.execute("delete from response_feedback")
            raise Refused("broken candidate")
        return start(peer, sha)

    monkeypatch.setattr(rtx, "start", broken_start)

    with pytest.raises(Refused, match="broken candidate"):
        promote(fixture)

    assert target.current.resolve().name == OLD
    assert database_evidence(target)["counts"]["response_feedback"] == 1
    record = json.loads(next(transactions.glob("*/transaction.json")).read_text())
    assert record["status"] == "rolled_back"
    assert Path(record["backup"]["path"]).is_file()


def test_candidate_preflight_failure_does_not_stop_target(fixture, monkeypatch):
    target, events, _, transactions = fixture
    before = digest(target.db), target.current.resolve()

    def refuse(*_):
        raise Refused("artifact mismatch")

    monkeypatch.setattr(rtx, "accepted", refuse)
    with pytest.raises(Refused, match="artifact mismatch"):
        promote(fixture)

    assert (digest(target.db), target.current.resolve()) == before
    assert events == []
    assert not list(transactions.glob("*/transaction.json"))


def test_recover_marks_preboundary_transaction_refused_without_mutation(fixture, monkeypatch):
    target, events, _, transactions = fixture
    tx_id = "external-recovery-001"
    directory = transactions / tx_id
    directory.mkdir(parents=True)
    external_rtx.ExternalJournal.create(
        directory / "transaction.json",
        target=target,
        expected=OLD,
        old=str(target.current.resolve()),
        accepted=str(Path("/tmp/releases") / NEW),
        artifact_digest="c" * 64,
        transaction_id=tx_id,
    )
    monkeypatch.setattr(rtx, "trusted", lambda _: None)

    result = external_rtx.recover(target, tx_id)

    assert result["status"] == "refused"
    assert result["recovery"] == "confirmed no active mutation occurred"
    assert target.current.resolve().name == OLD
    assert events == []


def test_external_controller_guard_rejects_instance_identity(monkeypatch):
    monkeypatch.setattr(external_rtx.socket, "gethostname", lambda: "rtx-omnigent")
    monkeypatch.setattr(external_rtx.os, "geteuid", lambda: 0)
    monkeypatch.setenv("OMNIGENT_INSTANCE_ID", "O1")

    with pytest.raises(Refused, match="instance-controlled"):
        external_rtx.external_controller_guard()
