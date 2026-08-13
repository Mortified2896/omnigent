"""Fail-closed host orchestration for either Control Room peer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from . import acceptance, baseline, identity, preflight, reconcile, staging, transaction
from .acceptance import CandidateAcceptance
from .identity import Instance
from .mode import DeploymentMode

TERMINAL_PHASES = {"tx_committed", "rolled_back", "failure"}
DEFAULT_EVIDENCE_ROOT = Path("/var/lib/omnigent-control-room/evidence")


class PromotionError(RuntimeError):
    pass


class PromotionInterrupted(PromotionError):
    pass


def _run(
    argv: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd="/tmp", env=env, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise PromotionError(
            f"command failed rc={result.returncode}: {argv!r}; stderr={result.stderr[-1500:]!r}"
        )
    return result


def _svc(unit: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["systemctl", *args, unit], check=check)


def _active(unit: str) -> bool:
    return _svc(unit, "is-active", check=False).stdout.strip() == "active"


def _health(url: str) -> str:
    return _run(["curl", "-fsS", "--max-time", "5", url]).stdout.strip()


def _wait_health(url: str, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _run(["curl", "-fsS", "--max-time", "3", url], check=False).returncode == 0:
            return
        time.sleep(1)
    raise PromotionError(f"health timeout: {url}")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _db(path: Path) -> tuple[str, str, dict[str, int]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        schema = str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        counts = {
            name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in ("conversations", "items", "labels")
            if name in tables
        }
    return integrity, schema, counts


def _validate_evidence_root(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise PromotionError("evidence directory must be an absolute traversal-free path")
    try:
        path.relative_to(DEFAULT_EVIDENCE_ROOT)
    except ValueError as exc:
        raise PromotionError(
            f"evidence directory must be beneath {DEFAULT_EVIDENCE_ROOT}"
        ) from exc
    DEFAULT_EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    for component in (
        DEFAULT_EVIDENCE_ROOT,
        *path.parents[: len(path.parents) - len(DEFAULT_EVIDENCE_ROOT.parents)],
    ):
        if component.exists() or component.is_symlink():
            metadata = component.lstat()
            if component.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise PromotionError(f"unsafe evidence directory component: {component}")
    return path


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _target_snapshot(target: Instance) -> dict[str, Any]:
    if not _active(target.service_unit) or not _active(target.host_unit):
        raise PromotionError(f"{target.name} target services are not both active")
    venv = target.deployment_root / "venv"
    runtime = identity.runtime_identity(venv / "bin" / "python")
    db_path = preflight.target_home_for(target) / "chat.db"
    integrity, schema, counts = _db(db_path)
    if integrity != "ok":
        raise PromotionError(f"{target.name} target DB integrity={integrity}")
    return {
        "runtime": runtime,
        "schema": schema,
        "integrity": integrity,
        "counts": counts,
        "health": _health(target.health_url),
    }


def _no_live_transactions(target: Instance, supervisor: Instance) -> None:
    root = transaction.DEFAULT_TX_ROOT
    if not root.is_dir():
        return
    blockers: list[str] = []
    for directory in sorted(root.iterdir()):
        path = directory / "transaction.json"
        if not path.is_file():
            continue
        try:
            blob = json.loads(path.read_text())
            phase = str(blob.get("phase", ""))
            tx_id = str(blob.get("tx_id", directory.name))
        except Exception:  # noqa: BLE001 - corrupt records must block
            blockers.append(f"{directory.name}:corrupt")
            continue
        if phase in TERMINAL_PHASES:
            continue
        try:
            result = reconcile.validate_completed_reconciliation(
                tx_id,
                tx_root=root,
                quarantine_root=reconcile.DEFAULT_QUARANTINE_ROOT,
                allowed_target=target,
                allowed_supervisor=supervisor,
            )
        except Exception as exc:  # noqa: BLE001 - validator errors must block
            blockers.append(f"{tx_id}:{phase}:validator:{exc}")
            continue
        if not result.is_validly_reconciled:
            blockers.append(f"{tx_id}:{phase}")
    if blockers:
        raise PromotionError("non-terminal transaction exists: " + ", ".join(blockers))


def _backup_db(source: Path, destination: Path) -> str:
    incoming = sqlite3.connect(str(source))
    outgoing = sqlite3.connect(str(destination))
    try:
        with outgoing:
            incoming.backup(outgoing)
    finally:
        outgoing.close()
        incoming.close()
    if _db(destination)[0] != "ok":
        raise PromotionError("DB backup integrity failed")
    return _sha(destination)


def _metadata_snapshot(target: Instance, evidence: Path) -> dict[str, bool]:
    state: dict[str, bool] = {}
    root = evidence / "metadata-before"
    root.mkdir()
    for name in ("PROVENANCE.txt", "DEPLOYED_SHA"):
        source = target.deployment_root / name
        state[name] = source.is_file()
        if source.is_file():
            shutil.copy2(source, root / name)
    _atomic_json(root / "state.json", state)
    return state


def _restore_metadata(target: Instance, evidence: Path, state: dict[str, bool]) -> None:
    root = evidence / "metadata-before"
    for name, existed in state.items():
        destination = target.deployment_root / name
        if existed:
            source = root / name
            if not source.is_file():
                raise PromotionError(f"metadata backup missing: {source}")
            temporary = destination.with_name(f".{name}.rollback.{os.getpid()}")
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        elif destination.exists() or destination.is_symlink():
            destination.unlink()


def _stop_target(target: Instance) -> None:
    _svc(target.host_unit, "stop", check=False)
    _svc(target.service_unit, "stop", check=False)
    time.sleep(1)
    if _active(target.host_unit) or _active(target.service_unit):
        raise PromotionError(f"{target.name} services did not stop")


def _start_target(target: Instance) -> None:
    _svc(target.service_unit, "start")
    _wait_health(target.health_url)
    _svc(target.host_unit, "start")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not _active(target.host_unit):
        time.sleep(1)
    if not _active(target.host_unit):
        raise PromotionError(f"{target.name} host did not start")


def _wheel_hash(record: CandidateAcceptance, role: str) -> str:
    return next(item.sha256 for item in record.wheels if item.role == role)


def _prepare_record(
    *,
    tx_id: str,
    evidence: Path,
    target: Instance,
    supervisor: Instance,
    mode: DeploymentMode,
    accepted: CandidateAcceptance,
    acceptance_path: Path,
    supervisor_baseline: baseline.SupervisorBaseline,
    target_before: dict[str, Any],
) -> tuple[transaction.TransactionRecord, str]:
    target_venv = target.deployment_root / "venv"
    runtime = target_before["runtime"]
    layout = "symlink" if target_venv.is_symlink() else "directory"
    old_path = (
        str(target_venv.resolve())
        if layout == "symlink"
        else str(target.deployment_root / f"venv.legacy-{tx_id}")
    )
    record = transaction.create(
        tx_id=tx_id,
        target=target.name,
        supervisor=supervisor.name,
        target_artifact_sha=accepted.source_sha,
        target_artifact_version=accepted.package_version,
        main_wheel_sha256=_wheel_hash(accepted, "main"),
        sdk_client_wheel_sha256=_wheel_hash(accepted, "sdk_client"),
        sdk_ui_wheel_sha256=_wheel_hash(accepted, "sdk_ui"),
        mode=mode.value,
        acceptance_record_path=str(acceptance_path),
        acceptance_record_sha256=accepted.acceptance_record_sha256,
        frontend_tree_sha256=accepted.frontend_tree_sha256,
        supervisor_baseline=supervisor_baseline.to_dict(),
        exact_wheels=[asdict_wheel(item) for item in accepted.wheels],
        referenced_resources=[
            str(acceptance_path),
            old_path,
            str(preflight.target_home_for(target) / "chat.db"),
            str(target.deployment_root / "PROVENANCE.txt"),
            str(target.deployment_root / "DEPLOYED_SHA"),
        ],
    )
    record.old_runtime_path = old_path
    record.old_runtime_sha = str(runtime.get("commit_sha", ""))
    record.old_runtime_version = str(runtime.get("version", ""))
    record.new_runtime_path = str(target.deployment_root / "releases" / accepted.source_sha)
    record.new_runtime_sha = accepted.source_sha
    record.new_runtime_version = accepted.package_version
    record.old_db_schema = target_before["schema"]
    record.target_db_schema = accepted.target_db_schema
    record.log_path = str(evidence / "promotion.log")
    transaction.save(record)
    return record, layout


def asdict_wheel(wheel: acceptance.AcceptedWheel) -> dict[str, str]:
    return {"role": wheel.role, "filename": wheel.filename, "sha256": wheel.sha256}


def _candidate_venv(accepted: CandidateAcceptance, release: Path) -> Path:
    original = Path(accepted.runtime_venv_path)
    try:
        relative = original.relative_to(Path(accepted.immutable_release_root))
    except ValueError as exc:
        raise PromotionError("accepted runtime is outside immutable release") from exc
    return release / relative


def _stage_or_reference(
    record: transaction.TransactionRecord,
    *,
    supervisor: Instance,
    mode: DeploymentMode,
    accepted: CandidateAcceptance,
) -> tuple[Path, bool]:
    final = Path(record.new_runtime_path)
    if final.exists() or final.is_symlink():
        failures = acceptance.verify_release(
            accepted,
            final,
            enforce_bound_root=(mode is DeploymentMode.BOOTSTRAP_FIRST_PEER),
        )
        if failures:
            raise PromotionError(
                "pre-existing candidate does not match acceptance: " + "; ".join(failures)
            )
        transaction.register_referenced(record, str(final))
        return final, True
    if mode is DeploymentMode.BOOTSTRAP_FIRST_PEER:
        raise PromotionError(
            "bootstrap candidate must already exist at the target immutable release path"
        )
    source = preflight.source_release_for(mode, accepted, supervisor)
    transaction.register_referenced(record, str(source))
    wheels = accepted.wheel_map(source)
    if final.exists() or final.is_symlink():
        raise PromotionError(f"target release unexpectedly exists: {final}")
    transaction.advance(record, "candidate_staging")
    transaction.register_owned(record, str(final))
    staging.stage_candidate_runtime(
        final,
        supervisor,
        wheels,
        supervisor_release_root=source,
        accepted_frontend_root=Path(accepted.frontend_root),
    )
    failures = acceptance.verify_release(accepted, final, enforce_bound_root=False)
    if failures:
        raise PromotionError("staged candidate mismatch: " + "; ".join(failures))
    transaction.advance(record, "candidate_verified")
    return final, True


def _switch(
    record: transaction.TransactionRecord,
    *,
    target: Instance,
    candidate: Path,
    candidate_preexisting: bool,
    layout: str,
    accepted: CandidateAcceptance,
) -> Path:
    old = Path(record.old_runtime_path)
    final = Path(record.new_runtime_path)
    target_venv = target.deployment_root / "venv"
    if layout == "directory":
        if old.exists():
            raise PromotionError(f"transaction old-runtime path exists: {old}")
        os.rename(target_venv, old)
    else:
        if target_venv.resolve() != old:
            raise PromotionError("target venv changed after preflight")
        target_venv.unlink()
    if not candidate_preexisting:
        if final.exists() or final.is_symlink():
            raise PromotionError("final release appeared after staging")
        transaction.register_owned(record, str(final))
        os.rename(candidate, final)
    elif candidate != final:
        raise PromotionError("pre-existing candidate is not the target final release")
    candidate_venv = _candidate_venv(accepted, final)
    temporary = target.deployment_root / f".venv.tmp.{record.tx_id}"
    os.symlink(candidate_venv, temporary)
    os.replace(temporary, target_venv)
    provenance = final / "PROVENANCE.txt"
    if provenance.is_file():
        shutil.copy2(provenance, target.deployment_root / "PROVENANCE.txt")
    (target.deployment_root / "DEPLOYED_SHA").write_text(accepted.source_sha + "\n")
    return final


def _migrate(target: Instance, final: Path, accepted: CandidateAcceptance) -> None:
    database = preflight.target_home_for(target) / "chat.db"
    python = _candidate_venv(accepted, final) / "bin" / "python"
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "HOME": "/tmp",
    }
    _run(
        [
            str(python),
            "-m",
            "omnigent.db.migrate",
            "--database-url",
            f"sqlite:///{database}",
        ],
        env=environment,
    )
    integrity, schema, _ = _db(database)
    if integrity != "ok" or schema != accepted.target_db_schema:
        raise PromotionError(f"migration acceptance failed: {integrity}/{schema}")


def _accept(
    target: Instance,
    accepted: CandidateAcceptance,
    before_counts: dict[str, int],
) -> dict[str, Any]:
    target_venv = target.deployment_root / "venv"
    runtime = identity.runtime_identity(target_venv / "bin" / "python")
    database = preflight.target_home_for(target) / "chat.db"
    integrity, schema, counts = _db(database)
    if runtime.get("commit_sha") != accepted.source_sha:
        raise PromotionError(f"target runtime SHA mismatch: {runtime}")
    if runtime.get("version") != accepted.package_version:
        raise PromotionError(f"target runtime version mismatch: {runtime}")
    if integrity != "ok" or schema != accepted.target_db_schema:
        raise PromotionError(f"target DB mismatch: {integrity}/{schema}")
    for name, count in before_counts.items():
        if counts.get(name, -1) < count:
            raise PromotionError(f"target data count regressed: {name}")
    info = json.loads(
        _run(
            [
                "curl",
                "-fsS",
                "--max-time",
                "10",
                f"http://127.0.0.1:{target.port}/v1/info",
            ]
        ).stdout
    )
    if info.get("server_version") != accepted.package_version:
        raise PromotionError(f"target API version mismatch: {info}")
    return {
        "runtime": runtime,
        "integrity": integrity,
        "schema": schema,
        "counts": counts,
        "health": _health(target.health_url),
    }


def _restore_runtime(
    record: transaction.TransactionRecord,
    *,
    target: Instance,
    accepted: CandidateAcceptance,
    layout: str,
) -> None:
    old = Path(record.old_runtime_path)
    target_venv = target.deployment_root / "venv"
    candidate = _candidate_venv(accepted, Path(record.new_runtime_path))
    if target_venv.is_symlink():
        current = target_venv.resolve()
        if current not in (candidate.resolve(), old.resolve()):
            raise PromotionError(f"rollback refuses unknown runtime: {current}")
        target_venv.unlink()
    elif target_venv.exists():
        if layout != "directory":
            raise PromotionError("rollback refuses unknown non-symlink runtime")
        return
    if layout == "directory":
        if not old.is_dir():
            raise PromotionError(f"transaction old runtime missing: {old}")
        os.rename(old, target_venv)
    else:
        if not old.exists():
            raise PromotionError(f"old symlink target missing: {old}")
        temporary = target.deployment_root / f".venv.tmp.rollback.{record.tx_id}"
        os.symlink(old, temporary)
        os.replace(temporary, target_venv)


def _verify_rollback_backup(
    record: transaction.TransactionRecord,
    backup: Path,
    digest: str,
) -> None:
    if not transaction.is_owned(record, str(backup)):
        raise PromotionError(f"rollback DB backup is not transaction-owned: {backup}")
    if not backup.is_file() or _sha(backup) != digest or _db(backup)[0] != "ok":
        raise PromotionError("rollback DB backup failed verification")


def _restore_db(target: Instance, backup: Path, digest: str) -> None:
    database = preflight.target_home_for(target) / "chat.db"
    if not backup.is_file() or _sha(backup) != digest or _db(backup)[0] != "ok":
        raise PromotionError("rollback DB backup failed verification")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    temporary = database.with_name(database.name + f".rollback.{os.getpid()}")
    shutil.copy2(backup, temporary)
    os.replace(temporary, database)


def _rollback(
    record: transaction.TransactionRecord,
    *,
    reason: str,
    target: Instance,
    accepted: CandidateAcceptance,
    backup: Path,
    backup_digest: str,
    layout: str,
    metadata: dict[str, bool],
    evidence: Path,
) -> dict[str, Any]:
    _verify_rollback_backup(record, backup, backup_digest)
    if record.rollback_started:
        raise PromotionError("rollback already attempted for this transaction")
    record.phase = "failure"
    record.rollback_reason = reason
    record.rollback_started = True
    record.rollback_completed = False
    transaction.save(record)
    _stop_target(target)
    _restore_runtime(record, target=target, accepted=accepted, layout=layout)
    _restore_db(target, backup, backup_digest)
    _restore_metadata(target, evidence, metadata)
    _start_target(target)
    runtime = identity.runtime_identity(target.deployment_root / "venv" / "bin" / "python")
    integrity, schema, _ = _db(preflight.target_home_for(target) / "chat.db")
    if (
        runtime.get("commit_sha") != record.old_runtime_sha
        or runtime.get("version") != record.old_runtime_version
        or integrity != "ok"
        or schema != record.old_db_schema
    ):
        raise PromotionError(
            f"rollback verification failed: runtime={runtime} db={integrity}/{schema}"
        )
    record.phase = "rolled_back"
    record.rollback_completed = True
    transaction.save(record)
    return {"runtime": runtime, "integrity": integrity, "schema": schema}


def recover(record: transaction.TransactionRecord) -> dict[str, Any]:
    """Recover an interrupted promotion using its durable transaction record."""
    if not record.mutation_boundary_crossed:
        raise PromotionError("rollback refused before mutation boundary")
    if record.phase in {"tx_committed", "rolled_back"} or record.rollback_completed:
        raise PromotionError(f"rollback refused for terminal transaction phase {record.phase}")
    if not record.acceptance_record_path or not record.acceptance_record_sha256:
        raise PromotionError("new-mode rollback requires a recorded acceptance record")
    target = identity.get(record.target)
    supervisor = identity.get(record.supervisor)
    identity.require_distinct(target, supervisor)
    accepted = acceptance.load(
        record.acceptance_record_path,
        expected_hash=record.acceptance_record_sha256,
        require_immutable_permissions=True,
    )
    recorded_baseline = baseline.SupervisorBaseline.from_dict(record.supervisor_baseline)
    supervisor_before = baseline.capture(supervisor)
    drift = baseline.compare(recorded_baseline, supervisor_before)
    if drift:
        raise PromotionError("rollback supervisor baseline drift: " + "; ".join(drift))
    evidence = Path(record.log_path).parent
    state_path = evidence / "metadata-before" / "state.json"
    if not state_path.is_file():
        raise PromotionError(f"rollback metadata state missing: {state_path}")
    metadata = json.loads(state_path.read_text())
    if not isinstance(metadata, dict):
        raise PromotionError("rollback metadata state is invalid")
    backup = Path(record.db_backup_path)
    _verify_rollback_backup(record, backup, record.db_backup_sha256)
    if record.rollback_started:
        raise PromotionError("rollback already attempted for this transaction")
    record.rollback_started = True
    record.phase = "failure"
    transaction.save(record)
    layout = (
        "directory" if Path(record.old_runtime_path).name.startswith("venv.legacy-") else "symlink"
    )
    _stop_target(target)
    try:
        _restore_runtime(record, target=target, accepted=accepted, layout=layout)
        _restore_db(target, backup, record.db_backup_sha256)
        _restore_metadata(
            target, evidence, {str(key): bool(value) for key, value in metadata.items()}
        )
        _start_target(target)
    except BaseException:
        record.phase = "failure"
        record.rollback_completed = False
        transaction.save(record)
        raise
    runtime = identity.runtime_identity(target.deployment_root / "venv" / "bin" / "python")
    integrity, schema, _ = _db(preflight.target_home_for(target) / "chat.db")
    if (
        runtime.get("commit_sha") != record.old_runtime_sha
        or runtime.get("version") != record.old_runtime_version
        or integrity != "ok"
        or schema != record.old_db_schema
    ):
        raise PromotionError(
            f"rollback verification failed: runtime={runtime} db={integrity}/{schema}"
        )
    supervisor_after = baseline.capture(supervisor)
    drift = baseline.compare(recorded_baseline, supervisor_after)
    if drift:
        raise PromotionError("rollback supervisor drift: " + "; ".join(drift))
    record.phase = "rolled_back"
    record.rollback_completed = True
    transaction.save(record)
    return {
        "tx_id": record.tx_id,
        "target": target.name,
        "supervisor": supervisor.name,
        "runtime": runtime,
        "db_integrity": integrity,
        "db_schema": schema,
        "supervisor_zero_drift": True,
    }


def _signal(signum: int, _frame: Any) -> None:
    raise PromotionInterrupted(f"received signal {signum}")


def install_signal_handlers() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _signal)


def run(
    evidence_root: Path,
    *,
    promote: bool,
    target: Instance,
    supervisor: Instance,
    mode: DeploymentMode | str,
    acceptance_record_path: Path,
    storage_guard_probe: preflight.StorageGuardProbe = preflight.default_storage_guard_probe,
) -> int:
    """Preflight or promote with every identity and artifact input explicit."""
    if os.geteuid() != 0:
        raise PromotionError("must run as root outside Omnigent sandboxes")
    selected_mode = DeploymentMode.parse(mode)
    identity.require_distinct(target, supervisor)
    evidence_root = _validate_evidence_root(evidence_root)
    accepted = acceptance.load(acceptance_record_path)
    report = preflight.run_preflight(
        target=target,
        supervisor=supervisor,
        mode=selected_mode,
        acceptance_record_path=acceptance_record_path,
        acceptance_record=accepted,
        storage_guard_probe=storage_guard_probe,
    )
    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_json(evidence_root / "preflight.json", report.to_dict())
    if not report.passed:
        raise PromotionError("preflight failed; no target mutation performed")
    if not promote:
        print(
            json.dumps(
                {
                    "verdict": "READY — PREFLIGHT PASSED; NO TARGET MUTATION",
                    "target": target.name,
                    "supervisor": supervisor.name,
                    "mode": selected_mode.value,
                },
                indent=2,
            )
        )
        return 0

    _no_live_transactions(target, supervisor)
    supervisor_before = baseline.capture(supervisor)
    if selected_mode is DeploymentMode.PEER_COPY and (
        supervisor_before.artifact_sha != accepted.source_sha
        or supervisor_before.artifact_version != accepted.package_version
    ):
        raise PromotionError(
            "peer-copy supervisor baseline is not the accepted artifact: "
            f"{supervisor_before.artifact_sha}/{supervisor_before.artifact_version}"
        )
    target_before = _target_snapshot(target)
    _atomic_json(evidence_root / "supervisor-before.json", supervisor_before.to_dict())
    _atomic_json(evidence_root / "target-before.json", target_before)
    tx_id = transaction.make_tx_id()
    evidence = evidence_root / f"tx-{tx_id}"
    evidence.mkdir()
    record, layout = _prepare_record(
        tx_id=tx_id,
        evidence=evidence,
        target=target,
        supervisor=supervisor,
        mode=selected_mode,
        accepted=accepted,
        acceptance_path=acceptance_record_path,
        supervisor_baseline=supervisor_before,
        target_before=target_before,
    )
    transaction.advance(record, "preflight")
    metadata = _metadata_snapshot(target, evidence)
    transaction.advance(record, "db_backup")
    backup = evidence / "chat.db"
    backup_digest = _backup_db(preflight.target_home_for(target) / "chat.db", backup)
    record.db_backup_path = str(backup)
    record.db_backup_sha256 = backup_digest
    record.db_backup_integrity = "ok"
    transaction.register_owned(record, str(backup))
    candidate: Path | None = None
    crossed = False
    try:
        candidate, preexisting = _stage_or_reference(
            record,
            supervisor=supervisor,
            mode=selected_mode,
            accepted=accepted,
        )
        # Re-read immutable acceptance and exact candidate immediately before
        # crossing the active-runtime mutation boundary.
        accepted = acceptance.load(
            acceptance_record_path,
            expected_hash=record.acceptance_record_sha256,
            require_immutable_permissions=True,
        )
        failures = acceptance.verify_release(
            accepted,
            candidate,
            enforce_bound_root=(selected_mode is DeploymentMode.BOOTSTRAP_FIRST_PEER),
        )
        if failures:
            raise PromotionError("candidate drift before mutation: " + "; ".join(failures))
        guard_at_boundary = storage_guard_probe()
        if not guard_at_boundary.active or guard_at_boundary.latched:
            raise PromotionError(
                "storage guard blocked mutation boundary: "
                f"active={guard_at_boundary.active} latched={guard_at_boundary.latched}"
            )
        supervisor_at_boundary = baseline.capture(supervisor)
        boundary_drift = baseline.compare(supervisor_before, supervisor_at_boundary)
        if boundary_drift:
            raise PromotionError("supervisor drift before mutation: " + "; ".join(boundary_drift))
        transaction.cross_mutation_boundary(record)
        crossed = True
        transaction.advance(record, "switch")
        _stop_target(target)
        final = _switch(
            record,
            target=target,
            candidate=candidate,
            candidate_preexisting=preexisting,
            layout=layout,
            accepted=accepted,
        )
        _migrate(target, final, accepted)
        transaction.advance(record, "service_restart")
        _start_target(target)
        transaction.advance(record, "acceptance")
        target_after = _accept(target, accepted, target_before["counts"])
        _atomic_json(evidence / "target-after.json", target_after)
        supervisor_after = baseline.capture(supervisor)
        drift = baseline.compare(supervisor_before, supervisor_after)
        _atomic_json(evidence_root / "supervisor-after.json", supervisor_after.to_dict())
        if drift:
            raise PromotionError("supervisor baseline drift: " + "; ".join(drift))
        transaction.complete(record)
        result = {
            "verdict": "COMPLETE — PEER-SUPERVISED PROMOTION",
            "tx_id": tx_id,
            "target": target.name,
            "supervisor": supervisor.name,
            "mode": selected_mode.value,
            "sha": accepted.source_sha,
            "version": accepted.package_version,
        }
        _atomic_json(evidence / "SUMMARY.json", result)
        print(json.dumps(result, indent=2))
        return 0
    except BaseException as exc:  # noqa: BLE001 - signals require rollback
        reason = f"{type(exc).__name__}: {exc}"
        if not crossed:
            transaction.fail_record(record, reason)
            if candidate is not None and transaction.is_owned(record, str(candidate)):
                if candidate == Path(record.new_runtime_path) and candidate.is_dir():
                    shutil.rmtree(candidate)
                else:
                    staging.safe_cleanup_staging(target, tx_id)
            result = {
                "verdict": "STOP — PRE-MUTATION FAILURE; ACTIVE TARGET UNTOUCHED",
                "tx_id": tx_id,
                "reason": reason,
            }
            _atomic_json(evidence / "SUMMARY.json", result)
            print(json.dumps(result, indent=2))
            return 2
        try:
            restored = _rollback(
                record,
                reason=reason,
                target=target,
                accepted=accepted,
                backup=backup,
                backup_digest=backup_digest,
                layout=layout,
                metadata=metadata,
                evidence=evidence,
            )
            supervisor_after = baseline.capture(supervisor)
            drift = baseline.compare(supervisor_before, supervisor_after)
            result = {
                "verdict": "ROLLED BACK — PEER PROMOTION FAILED SAFELY",
                "tx_id": tx_id,
                "reason": reason,
                "rollback": restored,
                "supervisor_zero_drift": not drift,
                "supervisor_drift": drift,
            }
            _atomic_json(evidence / "SUMMARY.json", result)
            print(json.dumps(result, indent=2))
            return 3
        except BaseException as rollback_error:  # noqa: BLE001 - preserve evidence
            record.phase = "failure"
            record.rollback_completed = False
            record.rollback_reason = reason + f"; rollback failed: {rollback_error}"
            transaction.save(record)
            result = {
                "verdict": "CRITICAL — PROMOTION AND AUTOMATIC ROLLBACK FAILED",
                "tx_id": tx_id,
                "reason": reason,
                "rollback_failure": f"{type(rollback_error).__name__}: {rollback_error}",
            }
            _atomic_json(evidence / "SUMMARY.json", result)
            print(json.dumps(result, indent=2))
            return 4


__all__ = [
    "DeploymentMode",
    "PromotionError",
    "PromotionInterrupted",
    "install_signal_handlers",
    "recover",
    "run",
]
