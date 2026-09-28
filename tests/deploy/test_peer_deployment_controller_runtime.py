"""Disposable O1 -> O2 controller mutation and recovery coverage."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from deploy.scripts.peer_deployer import controller as controller_module
from deploy.scripts.peer_deployer import rtx
from deploy.scripts.peer_deployer.controller import (
    ControllerService,
    ControllerStore,
    RtxControllerAdapter,
)
from deploy.scripts.peer_deployer.rtx_contract import Peer, Refused, database_evidence
from deploy.scripts.peer_deployer.sync_plan import ACCEPTANCE_CHECKS

NOW = 1_000.0
SOURCE_SHA = "a" * 40
TARGET_SHA = "b" * 40


class _HealthResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> _HealthResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._stream.close()


def _database(peer: Peer, *, marker: str = "accepted user write") -> None:
    (peer.root / "state").mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(peer.db) as connection:
        connection.executescript(
            """
            CREATE TABLE rtx_instance_identity(instance TEXT, identity TEXT);
            CREATE TABLE alembic_version(version_num TEXT);
            CREATE TABLE conversations(id INTEGER PRIMARY KEY, live_status TEXT, marker TEXT);
            CREATE TABLE conversation_items(id INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE response_feedback(id INTEGER PRIMARY KEY, value TEXT);
            """
        )
        connection.execute(
            "INSERT INTO rtx_instance_identity VALUES (?, ?)",
            (peer.instance, peer.database_id),
        )
        connection.execute("INSERT INTO alembic_version VALUES ('schema-1')")
        connection.execute(
            "INSERT INTO conversations(live_status, marker) VALUES ('done', ?)",
            (marker,),
        )


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build two disposable, separately bound instances and a fake health API."""
    source = Peer("O1", tmp_path / "o1", 4097, 1111, "1" * 32, "3" * 32)
    target = Peer("O2", tmp_path / "o2", 4197, 2222, "2" * 32, "4" * 32)
    _database(source, marker="source state")
    _database(target)

    source_release = source.root / "releases" / SOURCE_SHA
    target_release = target.root / "releases" / TARGET_SHA
    candidate_release = tmp_path / "releases" / SOURCE_SHA
    source_release.mkdir(parents=True)
    target_release.mkdir(parents=True)
    candidate_release.mkdir(parents=True)
    source.current.symlink_to(source_release)
    target.current.symlink_to(target_release)
    (target.root / "state" / "controller-owned.txt").write_text("state")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(rtx, "ARTIFACTS", artifacts)
    monkeypatch.setattr(rtx, "load_peer", lambda name: {"O1": source, "O2": target}[name])
    monkeypatch.setattr(controller_module.os, "geteuid", lambda: 0)
    monkeypatch.delenv("OMNIGENT_INSTANCE_ID", raising=False)

    def record(sha: str, runtime: Path, *, schema: str = "schema-1") -> dict[str, Any]:
        return {
            "source_sha": sha,
            "runtime": str(runtime),
            "schema": schema,
            "schema_policy": "same-schema",
            "package_version": "0.17.4",
            "upstream_version": "0.17.0",
            "upstream_ref": "omnigent-ai/omnigent@" + "d" * 40,
            "checks": dict.fromkeys(ACCEPTANCE_CHECKS, True),
            "hashes": {"runtime.txt": "1" * 64},
            "rollback_compatibility": {
                "target_instance": "O2",
                "same_schema": True,
                "own_state_backup": True,
                "writes_fenced": True,
            },
        }

    source_record = record(SOURCE_SHA, candidate_release)
    target_record = record(TARGET_SHA, target_release)
    (artifacts / SOURCE_SHA).mkdir()
    (artifacts / TARGET_SHA).mkdir()
    (artifacts / SOURCE_SHA / "acceptance-v2.json").write_text(
        json.dumps(source_record, sort_keys=True)
    )
    (artifacts / TARGET_SHA / "acceptance-v2.json").write_text(
        json.dumps(target_record, sort_keys=True)
    )

    def accepted(path: Path, expected_digest: str) -> dict[str, Any]:
        value = json.loads(path.read_text())
        if controller_module.canonical_digest(value) != expected_digest:
            raise Refused("accepted-artifact mismatch")
        return value

    monkeypatch.setattr(rtx, "accepted", accepted)

    def snapshot(peer: Peer, expected_sha: str) -> dict[str, Any]:
        if peer.current.resolve().name != expected_sha:
            raise Refused("wrong expected-current SHA")
        database = database_evidence(peer)
        database.pop("counts")
        generation = "source-generation" if peer.instance == "O1" else "target-generation"
        return {
            "server": {
                "MainPID": "101" if peer.instance == "O1" else "102",
                "ActiveEnterTimestampMonotonic": generation,
            },
            "host": {
                "MainPID": "201" if peer.instance == "O1" else "202",
                "ActiveEnterTimestampMonotonic": generation,
            },
            "database": database,
            "info": {
                "instance_id": peer.instance,
                "build_sha": expected_sha,
                "deployment_write_fence_enabled": True,
                "smart_routing_enabled": True,
                "o3_routing_review_enabled": False,
            },
        }

    monkeypatch.setattr(rtx, "snapshot", snapshot)
    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: _HealthResponse(b'{"status":"ok"}'),
    )
    monkeypatch.setattr(controller_module, "_active_work", lambda _peer: 0)

    starts: list[tuple[str, str]] = []
    stops: list[str] = []
    monkeypatch.setattr(rtx, "stop", lambda peer: stops.append(peer.instance))

    def start(peer: Peer, sha: str) -> dict[str, Any]:
        starts.append((peer.instance, sha))
        return snapshot(peer, sha)

    monkeypatch.setattr(rtx, "start", start)

    transaction_root = tmp_path / "transactions"
    transaction_root.mkdir()
    adapter = RtxControllerAdapter(transaction_root=transaction_root)
    service = ControllerService(
        adapter=adapter,
        store=ControllerStore(tmp_path / "controller.sqlite3"),
        socket_path=tmp_path / "controller.sock",
        clock=lambda: NOW,
    )
    return {
        "adapter": adapter,
        "service": service,
        "source": source,
        "target": target,
        "artifacts": artifacts,
        "source_record": source_record,
        "starts": starts,
        "stops": stops,
        "candidate_release": candidate_release,
    }


def _enqueue(service: ControllerService, key: str) -> dict[str, Any]:
    plan = service.dispatch({"operation": "plan"})["plan"]
    assert plan["status"] == "ready"
    return service.dispatch(
        {
            "operation": "enqueue",
            "plan_id": plan["plan_id"],
            "idempotency_key": key,
            "requested_by": "admin@example.test",
        }
    )["job"]


def _transaction_record(runtime: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    paths = list(runtime["adapter"].transaction_root.glob("*/transaction.json"))
    assert len(paths) == 1
    path = paths[0]
    return path, json.loads(path.read_text())


def test_success_restarts_only_o2_and_preserves_own_state(runtime: dict[str, Any]) -> None:
    job = _enqueue(runtime["service"], "123e4567-e89b-42d3-a456-426614174010")
    result = runtime["service"].worker_once()

    assert result is not None
    assert result["status"] == "succeeded"
    assert runtime["starts"] == [("O2", SOURCE_SHA)]
    assert runtime["stops"] == ["O2"]
    assert runtime["target"].current.resolve() == runtime["candidate_release"]
    assert not (runtime["target"].root / "state" / "deployment-write-fence").exists()
    with sqlite3.connect(runtime["target"].db) as connection:
        assert connection.execute("SELECT marker FROM conversations").fetchone() == (
            "accepted user write",
        )
    _path, transaction = _transaction_record(runtime)
    assert transaction["status"] == "committed"
    assert transaction["writes_opened"] is True
    assert job["status"] == "queued"


def test_failed_backup_rolls_back_before_database_mutation(
    runtime: dict[str, Any], monkeypatch
) -> None:
    def fail_backup(*_args, **_kwargs):
        raise Refused("backup failed")

    monkeypatch.setattr(rtx, "backup", fail_backup)
    _enqueue(runtime["service"], "123e4567-e89b-42d3-a456-426614174011")
    result = runtime["service"].worker_once()

    assert result is not None
    assert result["status"] == "failed"
    assert "backup failed" in (result["reason"] or "")
    assert runtime["target"].current.resolve().name == TARGET_SHA
    assert not (runtime["target"].root / "state" / "deployment-write-fence").exists()
    _path, transaction = _transaction_record(runtime)
    assert transaction["status"] == "rolled_back"
    assert transaction["database_mutated"] is False


def test_startup_failure_restores_o2_database_and_release(
    runtime: dict[str, Any], monkeypatch
) -> None:
    real_start = rtx.start
    failed = False

    def fail_candidate_start(peer: Peer, sha: str) -> dict[str, Any]:
        nonlocal failed
        if peer.instance == "O2" and sha == SOURCE_SHA and not failed:
            failed = True
            with sqlite3.connect(peer.db) as connection:
                connection.execute(
                    "INSERT INTO conversations(live_status, marker) "
                    "VALUES ('done', 'failed candidate write')"
                )
            raise Refused("startup failure")
        return real_start(peer, sha)

    monkeypatch.setattr(rtx, "start", fail_candidate_start)
    _enqueue(runtime["service"], "123e4567-e89b-42d3-a456-426614174012")
    result = runtime["service"].worker_once()

    assert result is not None
    assert result["status"] == "failed"
    assert "startup failure" in (result["reason"] or "")
    assert runtime["target"].current.resolve().name == TARGET_SHA
    with sqlite3.connect(runtime["target"].db) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (1,)
        assert connection.execute("SELECT marker FROM conversations").fetchone() == (
            "accepted user write",
        )
    assert not (runtime["target"].root / "state" / "deployment-write-fence").exists()
    _path, transaction = _transaction_record(runtime)
    assert transaction["status"] == "rolled_back"


def test_failed_rollback_preserves_fence_and_requires_recovery(
    runtime: dict[str, Any], monkeypatch
) -> None:
    def fail_start(_peer: Peer, _sha: str) -> dict[str, Any]:
        raise Refused("start failed")

    def fail_rollback(*_args: Any) -> None:
        raise Refused("rollback failed")

    monkeypatch.setattr(rtx, "start", fail_start)
    monkeypatch.setattr(rtx, "rollback", fail_rollback)
    _enqueue(runtime["service"], "123e4567-e89b-42d3-a456-426614174013")
    result = runtime["service"].worker_once()

    assert result is not None
    assert result["status"] == "recovery_required"
    assert "rollback failed" in (result["reason"] or "")
    fence = runtime["target"].root / "state" / "deployment-write-fence"
    assert fence.exists()
    _path, transaction = _transaction_record(runtime)
    assert transaction["status"] == "recovery_required"
    assert "rollback_error" in transaction


def test_pinned_acceptance_digest_drift_is_rejected_before_mutation(
    runtime: dict[str, Any],
) -> None:
    key = "123e4567-e89b-42d3-a456-426614174014"
    _enqueue(runtime["service"], key)
    job = runtime["service"].store.get_by_idempotency(key)
    assert job is not None
    acceptance_path = runtime["artifacts"] / SOURCE_SHA / "acceptance-v2.json"
    changed = json.loads(acceptance_path.read_text())
    changed["upstream_version"] = "0.17.1"
    acceptance_path.write_text(json.dumps(changed, sort_keys=True))

    with pytest.raises(Refused, match="release digest changed"):
        runtime["adapter"].execute(job, now=NOW)

    assert runtime["target"].current.resolve().name == TARGET_SHA
    assert not (runtime["target"].root / "state" / "deployment-write-fence").exists()


def test_schema_change_blocks_fresh_plan_before_enqueue(runtime: dict[str, Any]) -> None:
    with sqlite3.connect(runtime["target"].db) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'schema-2'")
    plan = runtime["service"].dispatch({"operation": "plan"})["plan"]

    assert plan["status"] == "blocked"
    assert "peer_schema_mismatch" in plan["blockers"]
    assert plan["plan_id"] is None
