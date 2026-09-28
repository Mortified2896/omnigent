"""Disposable-controller coverage for the O1 -> O2 action boundary."""

from __future__ import annotations

import fcntl
import multiprocessing
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from deploy.scripts.peer_deployer.controller import (
    ControllerRejected,
    ControllerService,
    ControllerStore,
)
from deploy.scripts.peer_deployer.rtx_contract import Refused
from deploy.scripts.peer_deployer.sync_plan import (
    ACCEPTANCE_CHECKS,
    ControllerReadiness,
    PeerObservation,
    SyncPlan,
    acceptance_digest,
    plan_sync,
)

NOW = 1_000.0
SOURCE_SHA = "a" * 40
TARGET_SHA = "b" * 40
OTHER_TARGET_SHA = "c" * 40


def _ready_plan(target_sha: str = TARGET_SHA) -> SyncPlan:
    acceptance: dict[str, Any] = {
        "source_sha": SOURCE_SHA,
        "schema_policy": "same-schema",
        "schema": "schema-1",
        "checks": dict.fromkeys(ACCEPTANCE_CHECKS, True),
        "hashes": {"runtime/venv/bin/python": "1" * 64},
        "rollback_compatibility": {
            "target_instance": "O2",
            "same_schema": True,
            "own_state_backup": True,
            "writes_fenced": True,
        },
    }
    digest = acceptance_digest(acceptance)
    source = PeerObservation(
        instance="O1",
        source_sha=SOURCE_SHA,
        release_digest=digest,
        package_version="0.13.0",
        upstream_version="0.13.0",
        upstream_ref="omnigent-ai/omnigent@" + "d" * 40,
        generation="source-generation",
        database_id="1" * 32,
        schema="schema-1",
        observed_at=NOW,
        healthy=True,
        active_work=0,
        live_validated_digest=digest,
        live_validated_generation="source-generation",
    )
    target = PeerObservation(
        instance="O2",
        source_sha=target_sha,
        release_digest="2" * 64,
        package_version="0.12.0",
        generation="target-generation",
        database_id="2" * 32,
        schema="schema-1",
        observed_at=NOW,
        healthy=True,
        active_work=0,
    )
    return plan_sync(
        source,
        target,
        acceptance,
        ControllerReadiness(
            observed_at=NOW,
            independent=True,
            idle_guard_ready=True,
            write_fence_ready=True,
            rollback_ready=True,
            transaction_idle=True,
            verified_release_digest=digest,
        ),
        now=NOW,
    )


class FakeAdapter:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.current = _ready_plan()
        self.next_plan: SyncPlan | None = None
        self.executed: list[Mapping[str, Any]] = []
        self.model_calls = 0
        self.failure: BaseException | None = None

    @property
    def transaction_root(self) -> Path:
        return self._root

    def plan(self, *, now: float) -> SyncPlan:
        if self.next_plan is not None:
            plan, self.next_plan = self.next_plan, None
            return plan
        return self.current

    def execute(self, job: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        self.executed.append(job)
        if self.failure is not None:
            raise self.failure
        return {"transaction": "disposable", "source_sha": SOURCE_SHA}


def _hold_lock(root: str, ready: Any, release: Any) -> None:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    with (root_path / "deploy.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)


def _service(tmp_path: Path) -> tuple[ControllerService, FakeAdapter]:
    adapter = FakeAdapter(tmp_path / "transactions")
    return (
        ControllerService(
            adapter=adapter,
            store=ControllerStore(tmp_path / "controller.sqlite3"),
            socket_path=tmp_path / "controller.sock",
            clock=lambda: NOW,
        ),
        adapter,
    )


def test_plan_is_opaque_and_successful_duplicate_returns_same_job(tmp_path: Path) -> None:
    service, adapter = _service(tmp_path)
    response = service.dispatch({"operation": "plan"})
    plan = response["plan"]
    assert plan["plan_id"].startswith("plan-")
    assert plan["source"]["official_version"] == "0.13.0"
    assert plan["source"]["custom_version"] == f"git-{SOURCE_SHA[:12]}"
    assert "expected" not in plan
    assert "hashes" not in plan

    key = "123e4567-e89b-42d3-a456-426614174000"
    first = service.dispatch(
        {
            "operation": "enqueue",
            "plan_id": plan["plan_id"],
            "idempotency_key": key,
            "requested_by": "admin@example.test",
        }
    )["job"]
    duplicate = service.dispatch(
        {
            "operation": "enqueue",
            "plan_id": plan["plan_id"],
            "idempotency_key": key,
            "requested_by": "admin@example.test",
        }
    )["job"]
    assert duplicate["id"] == first["id"]
    assert duplicate["status"] == "queued"
    assert adapter.executed == []
    assert adapter.model_calls == 0


def test_same_idempotency_key_cannot_bind_to_different_identity(tmp_path: Path) -> None:
    service, adapter = _service(tmp_path)
    first_plan = service.dispatch({"operation": "plan"})["plan"]
    key = "123e4567-e89b-42d3-a456-426614174001"
    service.dispatch(
        {
            "operation": "enqueue",
            "plan_id": first_plan["plan_id"],
            "idempotency_key": key,
            "requested_by": "admin@example.test",
        }
    )
    adapter.current = _ready_plan(OTHER_TARGET_SHA)
    second_plan = service.dispatch({"operation": "plan"})["plan"]
    with pytest.raises(ControllerRejected, match="different release identity"):
        service.dispatch(
            {
                "operation": "enqueue",
                "plan_id": second_plan["plan_id"],
                "idempotency_key": key,
                "requested_by": "admin@example.test",
            }
        )


def test_worker_revalidates_drift_and_never_executes_after_busy_change(tmp_path: Path) -> None:
    service, adapter = _service(tmp_path)
    plan = service.dispatch({"operation": "plan"})["plan"]
    service.dispatch(
        {
            "operation": "enqueue",
            "plan_id": plan["plan_id"],
            "idempotency_key": "123e4567-e89b-42d3-a456-426614174002",
            "requested_by": "admin@example.test",
        }
    )
    adapter.next_plan = SyncPlan(
        status="blocked",
        blockers=("target_not_idle",),
        source=adapter.current.source,
        target=adapter.current.target,
        expires_at=NOW + 60,
    )
    job = service.worker_once()
    assert job is not None
    assert job["status"] == "failed"
    assert "sync is not ready" in (job["reason"] or "")
    assert adapter.executed == []


def test_routine_failure_is_durable_and_does_not_call_a_model(tmp_path: Path) -> None:
    service, adapter = _service(tmp_path)
    adapter.failure = Refused("artifact changed")
    plan = service.dispatch({"operation": "plan"})["plan"]
    service.dispatch(
        {
            "operation": "enqueue",
            "plan_id": plan["plan_id"],
            "idempotency_key": "123e4567-e89b-42d3-a456-426614174003",
            "requested_by": "admin@example.test",
        }
    )
    job = service.worker_once()
    assert job is not None
    assert job["status"] == "failed"
    assert "artifact changed" in (job["reason"] or "")
    assert adapter.model_calls == 0


def test_real_cross_process_deployment_lock_refuses_enqueue(tmp_path: Path) -> None:
    service, _adapter = _service(tmp_path)
    plan = service.dispatch({"operation": "plan"})["plan"]
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_lock, args=(str(tmp_path / "transactions"), ready, release)
    )
    holder.start()
    assert ready.wait(5)
    try:
        with pytest.raises(ControllerRejected, match="deployment already in flight"):
            service.dispatch(
                {
                    "operation": "enqueue",
                    "plan_id": plan["plan_id"],
                    "idempotency_key": "123e4567-e89b-42d3-a456-426614174004",
                    "requested_by": "admin@example.test",
                }
            )
    finally:
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.kill()


def test_expired_plan_is_refused_without_a_job(tmp_path: Path) -> None:
    service, _adapter = _service(tmp_path)
    plan = service.dispatch({"operation": "plan"})["plan"]
    service.clock = lambda: NOW + 61
    with pytest.raises(ControllerRejected, match="expired"):
        service.dispatch(
            {
                "operation": "enqueue",
                "plan_id": plan["plan_id"],
                "idempotency_key": "123e4567-e89b-42d3-a456-426614174005",
                "requested_by": "admin@example.test",
            }
        )


def test_controller_interruption_leaves_running_job_for_recovery(tmp_path: Path) -> None:
    service, adapter = _service(tmp_path)
    plan = service.dispatch({"operation": "plan"})["plan"]
    service.dispatch(
        {
            "operation": "enqueue",
            "plan_id": plan["plan_id"],
            "idempotency_key": "123e4567-e89b-42d3-a456-426614174006",
            "requested_by": "admin@example.test",
        }
    )
    adapter.failure = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        service.worker_once()

    row = service.store.get_by_idempotency("123e4567-e89b-42d3-a456-426614174006")
    assert row is not None
    assert row["status"] == "running"
    assert service.store.recover_running(now=NOW + 1) == 1
    recovered = service.store.get_by_idempotency("123e4567-e89b-42d3-a456-426614174006")
    assert recovered is not None
    assert recovered["status"] == "recovery_required"
