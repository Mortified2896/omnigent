"""Regression coverage for the independent RTX deployment controller."""

import json
import sqlite3
import stat
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
    artifact = {
        "source_sha": NEW,
        "runtime": str(new_release),
        "schema": "schema",
        "schema_policy": "same-schema",
        "hashes": {},
    }
    old_artifact = {
        "source_sha": OLD,
        "runtime": str(old_release),
        "schema": "schema",
        "schema_policy": "same-schema",
        "hashes": {},
    }

    transaction_root = tmp_path / "transactions"
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(external_rtx, "_RELEASE_ROOT", tmp_path / "releases")
    for sha, payload in ((OLD, old_artifact), (NEW, artifact)):
        directory = artifact_root / sha
        directory.mkdir(parents=True)
        (directory / "acceptance-v2.json").write_text(json.dumps(payload))
    monkeypatch.setattr(rtx, "TRANSACTIONS", transaction_root)
    monkeypatch.setattr(rtx, "ARTIFACTS", artifact_root)
    monkeypatch.setattr(external_rtx, "external_controller_guard", lambda: None)

    def accepted(path, _digest, **_kwargs):
        if Path(path).parent.name == NEW:
            return artifact
        if Path(path).parent.name == OLD:
            return old_artifact
        raise Refused("unknown fixture artifact")

    monkeypatch.setattr(rtx, "accepted", accepted)
    monkeypatch.setattr(external_rtx, "_run_as_service", lambda _args: None)
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
        rtx.ARTIFACTS / NEW / "acceptance-v2.json",
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

    def refuse(*_, **_kwargs):
        raise Refused("artifact mismatch")

    monkeypatch.setattr(rtx, "accepted", refuse)
    with pytest.raises(Refused, match="artifact mismatch"):
        promote(fixture)

    assert (digest(target.db), target.current.resolve()) == before
    assert events == []
    assert not list(transactions.glob("*/transaction.json"))


def test_promote_repairs_only_candidate_acceptance_permissions_before_stop(fixture, monkeypatch):
    _, events, _, _ = fixture
    artifact_root = rtx.ARTIFACTS
    candidate_directory = artifact_root / NEW
    candidate_record = candidate_directory / "acceptance-v2.json"
    original_bytes = candidate_record.read_bytes()
    artifact_root_mode = stat.S_IMODE(artifact_root.stat().st_mode)
    previous_directory = artifact_root / OLD
    previous_directory_mode = stat.S_IMODE(previous_directory.stat().st_mode)
    previous_record = previous_directory / "acceptance-v2.json"
    previous_record_mode = stat.S_IMODE(previous_record.stat().st_mode)
    candidate_directory.chmod(0o700)
    candidate_record.chmod(0o600)

    permission_checks = []

    def check_as_service(args):
        permission_checks.append(tuple(args))

    monkeypatch.setattr(external_rtx, "_run_as_service", check_as_service)

    def stop_after_permissions(peer):
        assert stat.S_IMODE(candidate_directory.stat().st_mode) == 0o755
        assert stat.S_IMODE(candidate_record.stat().st_mode) == 0o444
        assert candidate_record.read_bytes() == original_bytes
        events.append(("stop", peer.instance))

    monkeypatch.setattr(rtx, "stop", stop_after_permissions)
    result = promote(fixture)

    assert result["status"] == "committed"
    assert events[0] == ("stop", "O1")
    assert artifact_root_mode == stat.S_IMODE(artifact_root.stat().st_mode)
    assert previous_directory_mode == stat.S_IMODE(previous_directory.stat().st_mode)
    assert previous_record_mode == stat.S_IMODE(previous_record.stat().st_mode)
    assert permission_checks == [
        ("runuser", "-u", "hermes", "--", "test", "-x", str(candidate_directory)),
        ("runuser", "-u", "hermes", "--", "test", "-r", str(candidate_record)),
    ]


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


def test_committed_external_rollback_restores_release_and_dropin_without_db_restore(
    fixture, monkeypatch
):
    target, events, _, transactions = fixture
    dropin = target.root / "unit.d" / "50-tailscale-auth-overlay.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Service]\nEnvironment=OMNIGENT_AUTH_TRUSTED_HEADER=Tailscale-User-Login\n")
    dropin_sha = digest(dropin)
    monkeypatch.setattr(external_rtx, "_auth_overlay_dropin_path", lambda _: dropin)
    monkeypatch.setattr(rtx, "trusted", lambda _: None)

    committed = promote(fixture, tx_id="external-auth-overlay-deploy-001")
    with sqlite3.connect(target.db) as connection:
        connection.execute("insert into conversations values ('disposable test session')")
    before = database_evidence(target)
    events.clear()
    monkeypatch.setattr(rtx, "run", lambda args, **_: events.append((args[0], *args[1:])))

    rolled_back = external_rtx.rollback_committed(
        target,
        "external-auth-overlay-deploy-001",
        "external-auth-overlay-rollback-001",
        dropin_sha,
    )

    assert committed["status"] == "committed"
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["database_restored"] is False
    assert target.current.resolve().name == OLD
    assert not dropin.exists()
    assert database_evidence(target) == before
    assert events == [
        ("stop", "O1"),
        ("systemctl", "daemon-reload"),
        ("start", "O1", OLD),
    ]
    original = json.loads(
        (transactions / "external-auth-overlay-deploy-001" / "transaction.json").read_text()
    )
    assert original["status"] == "committed"
    rollback_record = json.loads(
        (transactions / "external-auth-overlay-rollback-001" / "transaction.json").read_text()
    )
    assert rollback_record["source_transaction_id"] == "external-auth-overlay-deploy-001"
    assert rollback_record["database_after"] == before


def test_external_controller_guard_rejects_instance_identity(monkeypatch):
    monkeypatch.setattr(external_rtx.socket, "gethostname", lambda: "rtx-omnigent")
    monkeypatch.setattr(external_rtx.os, "geteuid", lambda: 0)
    monkeypatch.setenv("OMNIGENT_INSTANCE_ID", "O1")

    with pytest.raises(Refused, match="instance-controlled"):
        external_rtx.external_controller_guard()


def test_rehearsed_migration_checks_source_before_stopping(fixture):
    _, events, artifact, _ = fixture
    artifact.update(
        schema_policy="rehearsed-migration",
        schema="new-schema",
        migration={"from_schema": "wrong"},
    )
    with pytest.raises(Refused, match="DB schema mismatch"):
        promote(fixture)
    assert events == []


def test_failed_migration_restores_database_and_release(fixture):
    target, _, artifact, transactions = fixture
    artifact.update(
        schema_policy="rehearsed-migration",
        schema="new-schema",
        migration={"from_schema": "schema"},
    )
    with pytest.raises(Refused, match="post-start DB schema mismatch"):
        promote(fixture)
    assert target.current.resolve().name == OLD
    assert database_evidence(target)["schema"] == "schema"
    record = json.loads(next(transactions.glob("*/transaction.json")).read_text())
    assert record["status"] == "rolled_back"


def test_rehearsed_migration_commits_only_expected_schema(fixture, monkeypatch):
    target, _, artifact, _ = fixture
    artifact.update(
        schema_policy="rehearsed-migration",
        schema="new-schema",
        migration={"from_schema": "schema"},
    )

    def migrate(peer, sha):
        with sqlite3.connect(peer.db) as connection:
            connection.execute("update alembic_version set version_num='new-schema'")
        return rtx.snapshot(peer, sha)

    monkeypatch.setattr(rtx, "start", migrate)
    result = promote(fixture)
    assert result["status"] == "committed"
    assert database_evidence(target)["schema"] == "new-schema"
