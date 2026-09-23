"""Independent RTX controller for the fixed O1 -> O2 deployment action.

This process is host-owned and runs outside both Omnigent service lifecycles.
The web application talks to it only through a fixed Unix socket.  The
controller recollects all evidence under the deployment lock, stores opaque
plans and jobs durably, verifies the accepted bytes again, and keeps the O2
write fence in place until the candidate is accepted or a pre-write-open
rollback has completed.

The normal path never imports an Omnigent model/provider module and never
creates an AI supervisor conversation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.request import urlopen

from . import rtx
from .rtx_contract import Journal, Peer, Refused, canonical_digest, require
from .sync_plan import (
    ControllerReadiness,
    PeerObservation,
    SyncPlan,
    plan_sync,
    validate_sync_request,
)

SOCKET_PATH = Path("/run/omnigent-peer-controller/controller.sock")
STORE_PATH = Path("/srv/omnigent/peer-transactions/controller.sqlite3")
MAX_LINE_BYTES = 128 * 1024
JOB_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "recovery_required"})
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "recovery_required"})


class ControllerRejected(RuntimeError):
    """A fixed-scope controller request failed closed."""


class ControllerRecoveryRequired(RuntimeError):
    """A deployment needs operator recovery rather than automatic rollback."""


class ControllerAdapter(Protocol):
    """Live evidence and mutation seam used by the service and tests."""

    def plan(self, *, now: float) -> SyncPlan: ...

    def execute(self, job: Mapping[str, Any], *, now: float) -> dict[str, Any]: ...

    @property
    def transaction_root(self) -> Path: ...


def _canonical(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _safe_reason(exc: BaseException) -> str:
    """Bound deployment diagnostics without exposing environments or secrets."""
    text = " ".join(str(exc).split())
    text = re.sub(r"/(?:srv|etc|run|tmp|home)/[^ ]+", "<host-path>", text)
    return f"{type(exc).__name__}: {text[:240]}" if text else type(exc).__name__


def _job_payload(row: sqlite3.Row) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row["job_id"],
        "status": row["status"],
        "requested_by": row["requested_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "reason": row["reason"],
    }
    if row["status"] not in JOB_STATUSES:
        result["status"] = "recovery_required"
    return result


class ControllerStore:
    """Durable plans/jobs with SQLite transaction-level deduplication."""

    def __init__(self, path: Path = STORE_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deployment_plans (
                    plan_id TEXT PRIMARY KEY,
                    expected_json TEXT NOT NULL,
                    display_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployment_jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    requested_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS deployment_jobs_status_idx
                    ON deployment_jobs(status, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def save_plan(self, plan: SyncPlan, *, now: float) -> str:
        plan_id = f"plan-{secrets.token_hex(16)}"
        display = plan.display()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO deployment_plans VALUES (?, ?, ?, ?, ?)",
                (
                    plan_id,
                    json.dumps(plan.expected(), sort_keys=True, separators=(",", ":")),
                    json.dumps(display, sort_keys=True, separators=(",", ":")),
                    plan.expires_at,
                    now,
                ),
            )
        return plan_id

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployment_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "plan_id": row["plan_id"],
            "expected": json.loads(row["expected_json"]),
            "display": json.loads(row["display_json"]),
            "expires_at": row["expires_at"],
        }

    def get_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployment_jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return None if row is None else dict(row)

    def enqueue(
        self,
        *,
        idempotency_key: str,
        expected: Mapping[str, Any],
        expires_at: float,
        requested_by: str,
        now: float,
    ) -> dict[str, Any]:
        fingerprint = _canonical(expected)
        job_id = f"job-{secrets.token_hex(16)}"
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM deployment_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    if row["fingerprint"] != fingerprint:
                        raise ControllerRejected(
                            "idempotency key is already bound to a different release identity"
                        )
                    connection.execute("COMMIT")
                    return _job_payload(row)
                connection.execute(
                    """INSERT INTO deployment_jobs
                       (job_id, idempotency_key, fingerprint, expected_json,
                        expires_at, requested_by, status, created_at, updated_at, reason)
                       VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, NULL)""",
                    (
                        job_id,
                        idempotency_key,
                        fingerprint,
                        json.dumps(dict(expected), sort_keys=True, separators=(",", ":")),
                        expires_at,
                        requested_by,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM deployment_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                connection.execute("COMMIT")
        except sqlite3.IntegrityError:
            existing = self.get_by_idempotency(idempotency_key)
            if existing is None or existing["fingerprint"] != fingerprint:
                raise ControllerRejected(
                    "idempotency key reservation raced with another identity"
                ) from None
            return {
                "id": existing["job_id"],
                "status": existing["status"],
                "requested_by": existing["requested_by"],
                "created_at": existing["created_at"],
                "updated_at": existing["updated_at"],
                "reason": existing["reason"],
            }
        if row is None:
            raise ControllerRejected("job reservation failed")
        return _job_payload(row)

    def claim_next(self, *, now: float) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deployment_jobs WHERE status = 'queued' "
                "ORDER BY created_at, job_id LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                "UPDATE deployment_jobs SET status = 'running', updated_at = ? WHERE job_id = ?",
                (now, row["job_id"]),
            )
            connection.execute("COMMIT")
        result = dict(row)
        result["status"] = "running"
        result["updated_at"] = now
        return result

    def finish(
        self, job_id: str, *, status: str, reason: str | None, now: float
    ) -> dict[str, Any]:
        if status not in JOB_STATUSES or status == "queued":
            raise ControllerRejected("invalid job status")
        with self._connect() as connection:
            connection.execute(
                "UPDATE deployment_jobs SET status = ?, updated_at = ?, reason = ? "
                "WHERE job_id = ?",
                (status, now, reason, job_id),
            )
            row = connection.execute(
                "SELECT * FROM deployment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise ControllerRejected("deployment job not found")
        return _job_payload(row)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return None if row is None else _job_payload(row)

    def recover_running(self, *, now: float) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deployment_jobs SET status = 'recovery_required', updated_at = ?, "
                "reason = ? WHERE status = 'running'",
                (now, "controller restarted while job was running; operator review required"),
            )
            return cursor.rowcount


def _active_work(peer: Peer) -> int:
    """Count only statuses the controller explicitly understands.

    A missing/changed schema raises instead of being treated as idle.  That
    makes schema drift a visible blocker rather than a race-prone false zero.
    """
    with sqlite3.connect(f"file:{peer.db}?mode=ro", uri=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "conversations" not in tables:
            raise Refused("conversation activity table missing")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversations)").fetchall()
        }
        if "live_status" not in columns:
            raise Refused("conversation activity column missing")
        active = connection.execute(
            "SELECT count(*) FROM conversations WHERE lower(cast(live_status AS text)) IN "
            "('running', 'waiting')"
        ).fetchone()[0]
        if "scheduled_task_runs" in tables:
            run_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
            }
            if "status" not in run_columns:
                raise Refused("scheduled activity column missing")
            active += connection.execute(
                "SELECT count(*) FROM scheduled_task_runs "
                "WHERE lower(cast(status AS text)) = 'running'"
            ).fetchone()[0]
    return int(active)


def _generation(snapshot: Mapping[str, Any]) -> str:
    values = {
        "server_pid": snapshot["server"].get("MainPID"),
        "server_entered": snapshot["server"].get("ActiveEnterTimestampMonotonic"),
        "host_pid": snapshot["host"].get("MainPID"),
        "host_entered": snapshot["host"].get("ActiveEnterTimestampMonotonic"),
    }
    if any(value in (None, "", "0") for value in values.values()):
        raise Refused("process generation evidence missing")
    return _canonical(values)


@dataclass
class RtxControllerAdapter:
    """Live RTX host adapter; all paths come from trusted rtx manifests."""

    transaction_root: Path = rtx.TRANSACTIONS

    def _collect_peer(
        self, peer: Peer, *, now: float
    ) -> tuple[PeerObservation, dict[str, Any], dict[str, Any]]:
        current_sha: str | None = None
        snapshot: dict[str, Any] = {}
        acceptance: dict[str, Any] = {}
        release_digest: str | None = None
        generation: str | None = None
        info: dict[str, Any] = {}
        healthy = False
        active_work: int | None = None
        try:
            if not peer.current.is_symlink():
                raise Refused("current release pointer missing")
            observed_sha = peer.current.resolve().name
            if not re.fullmatch(r"[a-f0-9]{40}", observed_sha):
                raise Refused("invalid current release")
            current_sha = observed_sha
            snapshot = rtx.snapshot(peer, observed_sha)
            info_value = snapshot["info"]
            require(isinstance(info_value, dict), "invalid /v1/info evidence")
            info = info_value
            generation = _generation(snapshot)
            acceptance_path = rtx.ARTIFACTS / observed_sha / "acceptance-v2.json"
            acceptance_value = json.loads(acceptance_path.read_text())
            require(isinstance(acceptance_value, dict), "acceptance record must be an object")
            acceptance = acceptance_value
            release_digest = canonical_digest(acceptance)
            rtx.accepted(acceptance_path, release_digest)
            with urlopen(f"http://127.0.0.1:{peer.port}/health", timeout=5) as response:
                healthy = response.status == 200 and json.load(response).get("status") == "ok"
            active_work = _active_work(peer)
        except (
            AttributeError,
            IndexError,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
            sqlite3.Error,
            Refused,
            KeyError,
        ):
            pass
        database_value = snapshot.get("database", {})
        database = database_value if isinstance(database_value, dict) else {}
        observation = PeerObservation(
            instance=peer.instance,
            source_sha=current_sha,
            release_digest=release_digest,
            package_version=acceptance.get("package_version"),
            upstream_version=acceptance.get("upstream_version"),
            upstream_ref=acceptance.get("upstream_ref"),
            generation=generation,
            database_id=database.get("identity"),
            schema=database.get("schema"),
            observed_at=now,
            healthy=healthy,
            active_work=active_work,
            live_validated_digest=release_digest if healthy else None,
            live_validated_generation=generation if healthy else None,
        )
        return observation, acceptance, info

    def _transaction_idle(self) -> bool:
        # Lock ownership is established by the caller for enqueue/worker
        # paths.  Do not reacquire the same deployment lock from a plan helper:
        # that would create the exact nested-locking hazard this controller is
        # designed to avoid.
        if not self.transaction_root.is_dir() or self.transaction_root.is_symlink():
            return False
        for path in self.transaction_root.glob("*/transaction.json"):
            try:
                if path.is_symlink() or not path.is_file():
                    return False
                state = json.loads(path.read_text())
                if not isinstance(state, dict):
                    return False
                if state.get("status") not in {
                    "committed",
                    "rolled_back",
                    "refused",
                }:
                    return False
                if state.get("fence_release_pending") is True:
                    return False
            except (AttributeError, OSError, ValueError, TypeError):
                return False
        return True

    def plan(self, *, now: float) -> SyncPlan:
        try:
            source = rtx.load_peer("O1")
            target = rtx.load_peer("O2")
        except (KeyError, OSError, TypeError, ValueError, Refused):
            return plan_sync(
                PeerObservation(instance="O1", observed_at=now),
                PeerObservation(instance="O2", observed_at=now),
                {},
                ControllerReadiness(observed_at=now),
                now=now,
            )
        source_observation, acceptance, source_info = self._collect_peer(source, now=now)
        target_observation, _target_acceptance, target_info = self._collect_peer(target, now=now)
        rollback = (
            acceptance.get("rollback_compatibility") if isinstance(acceptance, dict) else None
        )
        rollback_ready = (
            isinstance(rollback, Mapping)
            and rollback.get("target_instance") == "O2"
            and rollback.get("same_schema") is True
            and rollback.get("own_state_backup") is True
            and rollback.get("writes_fenced") is True
        )
        # The controller is a separate host unit, and both running releases
        # must explicitly advertise the middleware contract before any action
        # can be enabled.  Missing fields remain a blocker for old releases.
        source_fence = source_info.get("deployment_write_fence_enabled") is True
        target_fence = target_info.get("deployment_write_fence_enabled") is True
        readiness = ControllerReadiness(
            observed_at=now,
            independent=os.geteuid() == 0 and os.environ.get("OMNIGENT_INSTANCE_ID") is None,
            idle_guard_ready=target_observation.active_work == 0,
            write_fence_ready=source_fence and target_fence,
            rollback_ready=rollback_ready,
            transaction_idle=self._transaction_idle(),
            verified_release_digest=source_observation.release_digest,
        )
        # Target acceptance is observed for diagnostics only; it never becomes
        # source evidence for this one-way action.
        _ = _target_acceptance
        plan = plan_sync(source_observation, target_observation, acceptance, readiness, now=now)
        if os.path.lexists(target.root / "state" / "deployment-write-fence"):
            plan = replace(
                plan,
                status="blocked",
                blockers=(*plan.blockers, "target_write_fence_active"),
            )
        return plan

    def execute(self, job: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        """Perform the fixed O1 -> O2 update; caller already holds rtx.locked."""
        expected = json.loads(job["expected_json"])
        require(isinstance(expected, dict), "pinned release identity is malformed")
        source_expected = expected.get("source")
        target_expected = expected.get("target")
        require(isinstance(source_expected, Mapping), "pinned source identity is malformed")
        require(isinstance(target_expected, Mapping), "pinned target identity is malformed")
        target = rtx.load_peer("O2")
        source = rtx.load_peer("O1")
        target_sha = target_expected.get("source_sha")
        source_sha = source_expected.get("source_sha")
        require(target_sha and source_sha, "pinned release identity missing")
        acceptance_path = rtx.ARTIFACTS / source_sha / "acceptance-v2.json"
        acceptance_record = json.loads(acceptance_path.read_text())
        require(isinstance(acceptance_record, dict), "acceptance record must be an object")
        acceptance_digest = canonical_digest(acceptance_record)
        require(
            acceptance_digest == source_expected.get("release_digest"),
            "accepted release digest changed",
        )
        candidate = rtx.accepted(acceptance_path, acceptance_digest)
        require(candidate.get("source_sha") == source_sha, "accepted source release changed")
        source_before = rtx.snapshot(source, source_sha)
        target_before = rtx.snapshot(target, target_sha)
        require(
            source_before["info"].get("deployment_write_fence_enabled") is True,
            "source write-fence support is missing",
        )
        require(
            target_before["info"].get("deployment_write_fence_enabled") is True,
            "target write-fence support is missing",
        )
        require(_active_work(target) == 0, "target is busy; no active work may be interrupted")
        require(
            candidate.get("schema") == target_before["database"]["schema"], "DB schema mismatch"
        )
        rollback = candidate.get("rollback_compatibility")
        require(
            isinstance(rollback, Mapping)
            and rollback.get("target_instance") == "O2"
            and rollback.get("same_schema") is True
            and rollback.get("own_state_backup") is True
            and rollback.get("writes_fenced") is True,
            "target-specific rollback compatibility is missing",
        )
        tx_id = f"sync-{now:.0f}-{secrets.token_hex(8)}"
        directory = self.transaction_root / tx_id
        directory.mkdir(mode=0o700, parents=True)
        tx = Journal.create(
            directory / "transaction.json",
            target=target,
            supervisor=source,
            expected=target_sha,
            old=str(target.current.resolve()),
            accepted=candidate["runtime"],
            artifact_digest=acceptance_digest,
        )
        fence = target.root / "state" / "deployment-write-fence"
        fence_created = False
        fence_removed = False
        try:
            _create_fence(fence, tx_id)
            fence_created = True
            tx.save(
                fence_path=str(fence),
                source_before=source_before,
                target_before=target_before,
                status="fenced",
            )
            require(rtx.snapshot(target, target_sha) == target_before, "target identity changed")
            require(_active_work(target) == 0, "target became busy after write fence")
            require(rtx.snapshot(source, source_sha) == source_before, "source identity changed")
            tx.save(mutation_boundary=True, status="stopping")
            rtx.stop(target)
            tx.save(backup=rtx.backup(target, directory), status="backed_up")
            candidate = rtx.accepted(acceptance_path, acceptance_digest)
            rtx.switch(target, Path(candidate["runtime"]), tx)
            tx.save(database_mutated=True, status="starting")
            after = rtx.start(target, source_sha)
            require(
                after["info"].get("deployment_write_fence_enabled") is True,
                "candidate fence support missing",
            )
            require(
                rtx.snapshot(source, source_sha) == source_before,
                "source changed during deployment",
            )
            tx.save(status="committed", fence_release_pending=True)
            _remove_fence(fence, tx_id)
            fence_removed = True
            tx.save(status="committed", fence_release_pending=False, writes_opened=True)
        except BaseException as exc:
            tx.save(error=_safe_reason(exc))
            if (
                tx.record.get("writes_opened")
                or fence_removed
                or (fence_created and not os.path.lexists(fence))
            ):
                tx.save(status="recovery_required", fence_release_pending=fence_removed)
                raise ControllerRecoveryRequired(_safe_reason(exc)) from exc
            if tx.record.get("mutation_boundary"):
                try:
                    rtx.rollback(target, tx)
                    if fence.exists() or os.path.lexists(fence):
                        _remove_fence(fence, tx.path.parent.name)
                    rollback_evidence = tx.record.get("rollback_evidence")
                    rollback_info = (
                        rollback_evidence.get("info")
                        if isinstance(rollback_evidence, Mapping)
                        else None
                    )
                    require(
                        isinstance(rollback_info, Mapping)
                        and rollback_info.get("deployment_write_fence_enabled") is True,
                        "rollback release lacks write-fence support",
                    )
                    require(
                        rtx.snapshot(source, source_sha) == source_before,
                        "source changed during rollback",
                    )
                    tx.save(status="rolled_back", fence_release_pending=False)
                except BaseException as rollback_error:  # noqa: BLE001 - preserve recovery evidence
                    tx.save(
                        status="recovery_required", rollback_error=_safe_reason(rollback_error)
                    )
                    raise ControllerRecoveryRequired(_safe_reason(rollback_error)) from exc
            else:
                try:
                    tx.save(status="refused")
                    if fence_created and os.path.lexists(fence):
                        _remove_fence(fence, tx.path.parent.name)
                    tx.save(fence_release_pending=False)
                except BaseException as fence_error:  # noqa: BLE001 - preserve recovery evidence
                    tx.save(
                        status="recovery_required",
                        fence_release_pending=True,
                        rollback_error=_safe_reason(fence_error),
                    )
                    raise ControllerRecoveryRequired(_safe_reason(fence_error)) from exc
            raise
        return {"status": "succeeded", "transaction": tx_id, "source_sha": source_sha}


def _create_fence(path: Path, transaction_id: str) -> None:
    require(path.parent.is_dir() and not path.parent.is_symlink(), "target state root missing")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (json.dumps({"transaction": transaction_id}) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _remove_fence(path: Path, transaction_id: str) -> None:
    require(not path.is_symlink() and path.is_file(), "deployment fence is not owned")
    try:
        record = json.loads(path.read_text())
    except (OSError, TypeError, ValueError) as exc:
        raise Refused("deployment fence ownership record is invalid") from exc
    require(
        isinstance(record, dict) and record.get("transaction") == transaction_id,
        "deployment fence belongs to another transaction",
    )
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class ControllerService:
    """Socket protocol plus an independent durable worker loop."""

    def __init__(
        self,
        *,
        adapter: ControllerAdapter,
        store: ControllerStore,
        socket_path: Path = SOCKET_PATH,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.socket_path = socket_path
        self.clock = clock
        self._stop = threading.Event()

    def dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = payload.get("operation")
        if operation == "plan" and set(payload) == {"operation"}:
            now = self.clock()
            plan = self.adapter.plan(now=now)
            plan_id = self.store.save_plan(plan, now=now) if plan.status == "ready" else None
            body = plan.display()
            body["object"] = "deployment"
            body["plan_id"] = plan_id
            return {"ok": True, "plan": body}
        if operation == "enqueue" and set(payload) == {
            "operation",
            "plan_id",
            "idempotency_key",
            "requested_by",
        }:
            return {"ok": True, "job": self._enqueue(payload)}
        if operation == "job" and set(payload) == {"operation", "job_id"}:
            job = self.store.get_job(str(payload["job_id"]))
            if job is None:
                raise ControllerRejected("deployment job not found")
            return {"ok": True, "job": job}
        raise ControllerRejected("unsupported controller operation")

    def _enqueue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        plan_id = payload["plan_id"]
        idempotency_key = payload["idempotency_key"]
        requested_by = payload["requested_by"]
        if not isinstance(plan_id, str) or not isinstance(idempotency_key, str):
            raise ControllerRejected("invalid enqueue identity")
        if not isinstance(requested_by, str) or not requested_by.isprintable() or not requested_by:
            raise ControllerRejected("invalid requesting identity")
        stored = self.store.get_plan(plan_id)
        if stored is None:
            raise ControllerRejected("deployment plan not found; refresh evidence")
        existing = self.store.get_by_idempotency(idempotency_key)
        fingerprint = _canonical(stored["expected"])
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise ControllerRejected(
                    "idempotency key is already bound to a different release identity"
                )
            return {
                "id": existing["job_id"],
                "status": existing["status"],
                "requested_by": existing["requested_by"],
                "created_at": existing["created_at"],
                "updated_at": existing["updated_at"],
                "reason": existing["reason"],
            }
        now = self.clock()
        if not now < stored["expires_at"]:
            raise ControllerRejected("deployment plan expired; refresh evidence")
        # One lock covers the fresh recollection and reservation.  The worker
        # takes the same lock later for the actual mutation, never nesting it.
        try:
            with rtx.locked(self.adapter.transaction_root):
                fresh = self.adapter.plan(now=now)
                request = {
                    "operation": "sync-o2-to-o1",
                    "expected": stored["expected"],
                    "expires_at": stored["expires_at"],
                    "idempotency_key": idempotency_key,
                }
                from .sync_plan import SyncRefused

                try:
                    validate_sync_request(request, fresh, now=now)
                except SyncRefused as exc:
                    raise ControllerRejected(str(exc)) from exc
                return self.store.enqueue(
                    idempotency_key=idempotency_key,
                    expected=stored["expected"],
                    expires_at=stored["expires_at"],
                    requested_by=requested_by,
                    now=now,
                )
        except Refused as exc:
            raise ControllerRejected(str(exc)) from exc

    def worker_once(self) -> dict[str, Any] | None:
        job = self.store.claim_next(now=self.clock())
        if job is None:
            return None
        job_id = job["job_id"]
        try:
            with rtx.locked(self.adapter.transaction_root):
                now = self.clock()
                fresh = self.adapter.plan(now=now)
                request = {
                    "operation": "sync-o2-to-o1",
                    "expected": json.loads(job["expected_json"]),
                    "expires_at": job["expires_at"],
                    "idempotency_key": job["idempotency_key"],
                }
                from .sync_plan import SyncRefused

                try:
                    validate_sync_request(request, fresh, now=now)
                except SyncRefused as exc:
                    raise ControllerRejected(str(exc)) from exc
                result = self.adapter.execute(job, now=now)
            return (
                self.store.finish(job_id, status="succeeded", reason=None, now=self.clock())
                | result
            )
        except Exception as exc:  # noqa: BLE001 - persist routine failures; crashes recover on restart
            reason = _safe_reason(exc)
            status = (
                "recovery_required" if isinstance(exc, ControllerRecoveryRequired) else "failed"
            )
            return self.store.finish(job_id, status=status, reason=reason, now=self.clock())

    def run_worker(self, *, poll_seconds: float = 0.5) -> None:
        self.store.recover_running(now=self.clock())
        while not self._stop.is_set():
            if self.worker_once() is None:
                self._stop.wait(poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def serve_forever(self) -> None:
        if self.socket_path.exists() or os.path.lexists(self.socket_path):
            if not self.socket_path.is_socket():
                raise ControllerRejected("controller socket path is not a socket")
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        worker = threading.Thread(
            target=self.run_worker, name="peer-deployment-worker", daemon=True
        )
        worker.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o660)
            listener.listen(8)
            try:
                while True:
                    connection, _ = listener.accept()
                    threading.Thread(
                        target=self._handle_connection,
                        args=(connection,),
                        daemon=True,
                    ).start()
            finally:
                self.stop()
                if os.path.lexists(self.socket_path):
                    self.socket_path.unlink()

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(2.0)
            data = bytearray()
            while len(data) < MAX_LINE_BYTES:
                chunk = connection.recv(min(65536, MAX_LINE_BYTES - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
            try:
                payload = json.loads(bytes(data).splitlines()[0])
                if not isinstance(payload, dict):
                    raise ControllerRejected("request must be an object")
                result = self.dispatch(payload)
            except (
                ControllerRejected,
                IndexError,
                Refused,
                OSError,
                ValueError,
                TypeError,
                KeyError,
            ) as exc:
                result = {"ok": False, "message": _safe_reason(exc)}
            connection.sendall(
                (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
            )


def main() -> None:
    if os.geteuid() != 0 or os.environ.get("OMNIGENT_INSTANCE_ID"):
        raise SystemExit("external root controller required")
    service = ControllerService(adapter=RtxControllerAdapter(), store=ControllerStore())
    service.serve_forever()


if __name__ == "__main__":
    main()
