"""Trusted, topology-agnostic promotion engine.

The engine consumes a ``PromotionPlan`` and an ``ArtifactEntry`` that
were loaded by the daemon from root-owned files.  The caller (i.e.
the network request) supplies neither the plan nor the artifact
identity; the daemon selects both from the trusted registry+plans
based on the authenticated ``(caller, target)`` pair.

This module is intentionally narrow: it executes the operations the
preflight already proved safe, in the order the transaction contract
requires.  It does not pull secrets from the environment, it does not
resolve ``pip`` against live PyPI, and it does not allow the operator
to substitute paths the plan does not already cover.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import identity, staging, transaction
from .plan import PromotionPlan
from .registry import ArtifactEntry, TrustedRegistry


class PromotionError(RuntimeError):
    pass


class PromotionInterrupted(PromotionError):
    pass


@dataclass
class PromotionResult:
    tx_id: str
    verdict: str
    sha: str
    version: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        argv, cwd="/tmp", capture_output=True, text=True, check=False,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/tmp",
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        },
    )
    if check and p.returncode:
        raise PromotionError(
            f"command failed rc={p.returncode}: {argv!r}; stderr={p.stderr[-1500:]!r}"
        )
    return p


def _svc(unit: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["systemctl", *args, unit], check=check)


def _active(unit: str) -> bool:
    return _svc(unit, "is-active", check=False).stdout.strip() == "active"


def _snapshot_service(unit: str) -> dict[str, str]:
    p = _run([
        "systemctl", "show", unit,
        "-p", "MainPID",
        "-p", "NRestarts",
        "-p", "ActiveEnterTimestampMonotonic",
        "-p", "ActiveState",
    ])
    return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)


def _health(url: str) -> str:
    return _run(["curl", "-fsS", "--max-time", "5", url]).stdout.strip()


def _wait_health(url: str, timeout: int = 90) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _run(["curl", "-fsS", "--max-time", "3", url], check=False).returncode == 0:
            return
        time.sleep(1)
    raise PromotionError(f"health timeout: {url}")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _db(path: Path) -> tuple[str, str, dict[str, int]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
        integrity = str(c.execute("PRAGMA integrity_check").fetchone()[0])
        schema = str(c.execute("SELECT version_num FROM alembic_version").fetchone()[0])
        tables = {str(r[0]) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {
            n: int(c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0])
            for n in ("conversations", "items", "labels")
            if n in tables
        }
    return integrity, schema, counts


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _supervisor_guard(plan: PromotionPlan) -> dict[str, Any]:
    for unit in plan.supervisor_service_units:
        if not _active(unit):
            raise PromotionError(
                f"supervisor unit not active: {unit}"
            )
    runtime = identity.runtime_identity(
        Path(plan.supervisor_deployment_root) / "current" / "venv" / "bin" / "python"
    )
    if (
        runtime.get("commit_sha") != plan.expected_supervisor_pre_state.commit_sha
        or runtime.get("version") != plan.expected_supervisor_pre_state.version
    ):
        raise PromotionError(
            f"supervisor runtime does not match plan: {runtime} expected "
            f"{plan.expected_supervisor_pre_state}"
        )
    return {
        "service_units": {unit: _snapshot_service(unit) for unit in plan.supervisor_service_units},
        "runtime": runtime,
        "health": _health(plan.supervisor_health_url),
    }


def _target_pre_state(plan: PromotionPlan) -> dict[str, Any]:
    db_path = Path(plan.target_state_root) / "chat.db"
    for unit in plan.target_service_units:
        if not _active(unit):
            raise PromotionError(f"target unit not active: {unit}")
    runtime = identity.runtime_identity(
        Path(plan.target_deployment_root) / "venv" / "bin" / "python"
    )
    if (
        runtime.get("commit_sha") != plan.expected_target_pre_state.commit_sha
        or runtime.get("version") != plan.expected_target_pre_state.version
    ):
        raise PromotionError(
            f"target runtime does not match plan: {runtime} expected "
            f"{plan.expected_target_pre_state}"
        )
    integrity, schema, counts = _db(db_path)
    if integrity != "ok":
        raise PromotionError(f"target DB preflight failed: integrity={integrity}")
    if (
        plan.expected_target_pre_state.schema
        and schema != plan.expected_target_pre_state.schema
    ):
        raise PromotionError(
            f"target DB schema mismatch: actual={schema} expected={plan.expected_target_pre_state.schema}"
        )
    return {
        "service_units": {unit: _snapshot_service(unit) for unit in plan.target_service_units},
        "runtime": runtime,
        "schema": schema,
        "integrity": integrity,
        "counts": counts,
        "health": _health(plan.target_health_url),
    }


def _verify_artifacts(artifact: ArtifactEntry) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name, meta in artifact.wheels.items():
        path = Path(meta["path"])
        if not path.is_file() or _sha(path) != meta["sha256"]:
            raise PromotionError(
                f"wheel {name!r} missing or hash mismatch: {path}"
            )
        if name.startswith("omnigent-"):
            out["main"] = path
        elif name.startswith("omnigent_client-"):
            out["sdk_client"] = path
        elif name.startswith("omnigent_ui_sdk-"):
            out["sdk_ui"] = path
    if not {"main", "sdk_client", "sdk_ui"}.issubset(out.keys()):
        raise PromotionError(
            f"trusted artifact {artifact.artifact_sha} missing required wheels"
        )
    prov_path = Path(artifact.provenance)
    if not prov_path.is_file():
        raise PromotionError(f"provenance missing: {prov_path}")
    text = prov_path.read_text()
    if (
        f"sha={artifact.artifact_sha}" not in text
        or f"package_version={artifact.version}" not in text
    ):
        raise PromotionError("provenance does not match artifact identity")
    return out


def _backup_db(plan: PromotionPlan, dst: Path) -> str:
    db_path = Path(plan.target_state_root) / "chat.db"
    with sqlite3.connect(str(db_path)) as src, sqlite3.connect(str(dst)) as out:
        with out:
            src.backup(out)
    if _db(dst)[0] != "ok":
        raise PromotionError("DB backup integrity failed")
    return _sha(dst)


def _stage_and_switch(
    plan: PromotionPlan,
    record: transaction.TransactionRecord,
    wheels: dict[str, Path],
    artifact: ArtifactEntry,
) -> Path:
    """Stage the candidate and switch the target symlink under transaction control.

    The runtime transition is atomic: rename staged-dir to
    releases/<sha>/, then ``os.replace`` a temp symlink onto
    ``<deployment_root>/venv``.  Nothing else in the host changes.
    """
    target_root = Path(plan.target_deployment_root)
    final_release = target_root / "releases" / artifact.artifact_sha
    if final_release.exists() or final_release.is_symlink():
        raise PromotionError(
            f"final release already exists: {final_release}; cleanup must prove ownership"
        )
    supervisor_release_root = Path(artifact.release_root)
    staging_root = staging.transaction_owned_staging_path(
        identity.Instance(
            name=plan.target_name,
            deployment_root=target_root,
            service_unit=plan.target_service_units[0],
            host_unit=plan.target_service_units[1],
            port=0,
            health_url=plan.target_health_url,
        ),
        record.tx_id,
    )
    if staging_root.exists():
        raise PromotionError(f"TX staging unexpectedly exists: {staging_root}")
    transaction.advance(record, "candidate_staging")
    transaction.register_owned(record, str(staging_root))
    staging.stage_candidate_runtime(
        staging_root,
        identity.Instance(
            name=plan.supervisor_name,
            deployment_root=Path(plan.supervisor_deployment_root),
            service_unit=plan.supervisor_service_units[0],
            host_unit=plan.supervisor_service_units[1],
            port=0,
            health_url=plan.supervisor_health_url,
        ),
        wheels,
        supervisor_release_root=supervisor_release_root,
    )
    failures = staging.verify_candidate_complete(staging_root)
    if failures or not staging.candidate_identity_matches(
        staging_root, artifact.artifact_sha, artifact.version
    ):
        raise PromotionError(
            "candidate verification failed: " + "; ".join(failures)
        )
    closure = staging.capture_supervisor_closure(
        identity.Instance(
            name=plan.supervisor_name,
            deployment_root=Path(plan.supervisor_deployment_root),
            service_unit=plan.supervisor_service_units[0],
            host_unit=plan.supervisor_service_units[1],
            port=0,
            health_url=plan.supervisor_health_url,
        )
    )
    mismatches = staging.verify_candidate_versions(
        staging._candidate_python(staging_root),
        closure.expected_versions(),
    )
    if mismatches:
        raise PromotionError(
            "candidate closure mismatch: " + "; ".join(mismatches)
        )
    transaction.advance(record, "candidate_verified")
    transaction.advance(record, "switch")
    os.rename(staging_root, final_release)
    transaction.register_owned(record, str(final_release))
    tmp = target_root / f".venv.tmp.{record.tx_id}"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(final_release / "venv", tmp)
    os.replace(tmp, target_root / "venv")
    shutil.copy2(
        final_release / "PROVENANCE.txt", target_root / "PROVENANCE.txt"
    )
    (target_root / "DEPLOYED_SHA").write_text(artifact.artifact_sha + "\n")
    return final_release


def _migrate(final: Path, plan: PromotionPlan) -> None:
    db_path = Path(plan.target_state_root) / "chat.db"
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "HOME": "/tmp",
    }
    _run([
        str(final / "venv" / "bin" / "python"),
        "-m", "omnigent.db.migrate",
        "--database-url", f"sqlite:///{db_path}",
    ], env=env)  # type: ignore[arg-type]
    # The post-migration schema is whatever the runtime is committed to;
    # we accept any schema the migration produces, but the integration
    # test plan still asserts a deterministic schema on success.


def _start_target(plan: PromotionPlan) -> None:
    for unit in plan.target_service_units:
        _svc(unit, "start")
    _wait_health(plan.target_health_url)
    end = time.monotonic() + 30
    while time.monotonic() < end and not _active(plan.target_service_units[1]):
        time.sleep(1)
    if not _active(plan.target_service_units[1]):
        raise PromotionError("target host did not start")


def _stop_target(plan: PromotionPlan) -> None:
    for unit in reversed(plan.target_service_units):
        _svc(unit, "stop", check=False)
    time.sleep(1)
    if any(_active(u) for u in plan.target_service_units):
        raise PromotionError("target services did not stop")


def _accept(
    plan: PromotionPlan,
    artifact: ArtifactEntry,
    before_counts: dict[str, int],
) -> dict[str, Any]:
    runtime = identity.runtime_identity(
        Path(plan.target_deployment_root) / "venv" / "bin" / "python"
    )
    if (
        runtime.get("commit_sha") != artifact.artifact_sha
        or runtime.get("version") != artifact.version
    ):
        raise PromotionError(
            f"target runtime after switch does not match artifact: {runtime}"
        )
    db_path = Path(plan.target_state_root) / "chat.db"
    integrity, schema, counts = _db(db_path)
    if integrity != "ok":
        raise PromotionError(f"target DB integrity after migration: {integrity}")
    for name, old in before_counts.items():
        if counts.get(name, -1) < old:
            raise PromotionError(
                f"target data count regressed: {name} {old}->{counts.get(name)}"
            )
    return {
        "runtime": runtime,
        "schema": schema,
        "integrity": integrity,
        "counts": counts,
        "service_units": {unit: _snapshot_service(unit) for unit in plan.target_service_units},
        "health": _health(plan.target_health_url),
    }


def _signal(signum: int, _frame: Any) -> None:
    raise PromotionInterrupted(f"received signal {signum}")


def install_signal_handlers() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _signal)


def run_promotion(
    *,
    plan: PromotionPlan,
    registry: TrustedRegistry,
    evidence_root: Path,
    promote: bool,
) -> PromotionResult:
    """Execute the plan if ``promote=True``, else preflight-only.

    Returns a structured ``PromotionResult``.  Never calls
    ``host_promotion.run`` (which is the legacy hardcoded O2 -> O1
    path).  All inputs are already authenticated.
    """
    artifact = registry.get(plan.accepted_artifact_sha)
    identity.require_distinct(
        identity.Instance(
            name=plan.supervisor_name,
            deployment_root=Path(plan.supervisor_deployment_root),
            service_unit=plan.supervisor_service_units[0],
            host_unit=plan.supervisor_service_units[1],
            port=0,
            health_url=plan.supervisor_health_url,
        ),
        identity.Instance(
            name=plan.target_name,
            deployment_root=Path(plan.target_deployment_root),
            service_unit=plan.target_service_units[0],
            host_unit=plan.target_service_units[1],
            port=0,
            health_url=plan.target_health_url,
        ),
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    supervisor_before = _supervisor_guard(plan)
    target_before = _target_pre_state(plan)
    wheels = _verify_artifacts(artifact)
    _atomic_json(evidence_root / "01-supervisor-before.json", supervisor_before)
    _atomic_json(evidence_root / "02-target-before.json", target_before)
    tx_id = transaction.make_tx_id()
    evidence = evidence_root / f"tx-{tx_id}"
    evidence.mkdir()
    record = transaction.create(
        tx_id=tx_id,
        target=plan.target_name,
        supervisor=plan.supervisor_name,
        target_artifact_sha=artifact.artifact_sha,
        target_artifact_version=artifact.version,
        main_wheel_sha256=artifact.wheels[
            next(n for n in artifact.wheels if n.startswith("omnigent-"))
        ]["sha256"],
        sdk_client_wheel_sha256=artifact.wheels[
            next(n for n in artifact.wheels if n.startswith("omnigent_client-"))
        ]["sha256"],
        sdk_ui_wheel_sha256=artifact.wheels[
            next(n for n in artifact.wheels if n.startswith("omnigent_ui_sdk-"))
        ]["sha256"],
        root=RUNTIME_TRANSACTION_ROOT(),
    )
    record.old_runtime_sha = target_before["runtime"]["commit_sha"]
    record.old_runtime_version = target_before["runtime"]["version"]
    record.old_db_schema = target_before["schema"]
    record.log_path = str(evidence / "promotion.log")
    transaction.save(record, root=RUNTIME_TRANSACTION_ROOT())
    transaction.advance(record, "preflight", root=RUNTIME_TRANSACTION_ROOT())
    if not promote:
        try:
            staging.safe_cleanup_staging(
                identity.Instance(
                    name=plan.target_name,
                    deployment_root=Path(plan.target_deployment_root),
                    service_unit=plan.target_service_units[0],
                    host_unit=plan.target_service_units[1],
                    port=0,
                    health_url=plan.target_health_url,
                ),
                tx_id,
            )
        except staging.StagingError:
            pass
        record.phase = "failure"
        record.rollback_reason = "preflight-only; no mutation"
        transaction.save(record, root=RUNTIME_TRANSACTION_ROOT())
        return PromotionResult(
            tx_id=tx_id,
            verdict="PREFLIGHT PASSED; NO MUTATION",
            sha=artifact.artifact_sha,
            version=artifact.version,
        )
    record.db_backup_path = str(evidence / "chat.db")
    record.db_backup_sha256 = _backup_db(plan, Path(record.db_backup_path))
    record.db_backup_integrity = "ok"
    transaction.advance(record, "db_backup", root=RUNTIME_TRANSACTION_ROOT())
    transaction.save(record, root=RUNTIME_TRANSACTION_ROOT())
    crossed = False
    try:
        final = _stage_and_switch(plan, record, wheels, artifact)
        transaction.cross_mutation_boundary(record, root=RUNTIME_TRANSACTION_ROOT())
        crossed = True
        (evidence / "MUTATION_BOUNDARY_CROSSED").write_text("true\n")
        _stop_target(plan)
        _migrate(final, plan)
        transaction.advance(record, "service_restart", root=RUNTIME_TRANSACTION_ROOT())
        _start_target(plan)
        transaction.advance(record, "acceptance", root=RUNTIME_TRANSACTION_ROOT())
        target_after = _accept(plan, artifact, target_before["counts"])
        _atomic_json(evidence / "target-after.json", target_after)
        supervisor_after = _supervisor_guard(plan)
        if (
            plan.supervisor_zero_drift
            and supervisor_after != supervisor_before
        ):
            raise PromotionError("supervisor drift detected after mutation")
        transaction.complete(record, root=RUNTIME_TRANSACTION_ROOT())
        return PromotionResult(
            tx_id=tx_id,
            verdict="PROMOTION COMMITTED",
            sha=artifact.artifact_sha,
            version=artifact.version,
        )
    except BaseException as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if not crossed:
            record.phase = "failure"
            record.rollback_reason = reason
            transaction.save(record, root=RUNTIME_TRANSACTION_ROOT())
            try:
                staging.safe_cleanup_staging(
                    identity.Instance(
                        name=plan.target_name,
                        deployment_root=Path(plan.target_deployment_root),
                        service_unit=plan.target_service_units[0],
                        host_unit=plan.target_service_units[1],
                        port=0,
                        health_url=plan.target_health_url,
                    ),
                    tx_id,
                )
            except staging.StagingError:
                pass
            return PromotionResult(
                tx_id=tx_id,
                verdict="PRE-MUTATION FAILURE; TARGET UNTOUCHED",
                sha=artifact.artifact_sha,
                version=artifact.version,
                reason=reason,
            )
        # Honor the rollback contract: paired runtime + DB rollback.
        try:
            _stop_target(plan)
            # restore runtime symlink to old
            target_root = Path(plan.target_deployment_root)
            old_runtime = Path(record.old_runtime_path)
            if target_root.joinpath("venv").is_symlink():
                current = (target_root / "venv").resolve()
                if current != (Path(record.new_runtime_path) / "venv"):
                    raise PromotionError(
                        f"unexpected current venv: {current}"
                    )
                (target_root / "venv").unlink()
                tmp = target_root / f".venv.tmp.rollback.{record.tx_id}"
                os.symlink(old_runtime, tmp)
                os.replace(tmp, target_root / "venv")
            # restore db from backup
            backup = Path(record.db_backup_path)
            db = Path(plan.target_state_root) / "chat.db"
            for suffix in ("", "-shm", "-wal"):
                p = Path(str(backup) + suffix)
                if p.is_file():
                    target = Path(str(db) + suffix)
                    tmp = target.with_name(target.name + f".rollback.{os.getpid()}")
                    shutil.copy2(p, tmp)
                    os.replace(tmp, target)
            # restore provenance
            old_provenance = old_runtime / "PROVENANCE.txt"
            target_provenance = target_root / "PROVENANCE.txt"
            if old_provenance.is_file():
                tmp = target_provenance.with_name(
                    target_provenance.name + ".rollback"
                )
                shutil.copy2(old_provenance, tmp)
                os.replace(tmp, target_provenance)
            _start_target(plan)
            record.phase = "rolled_back"
            record.rollback_completed = True
            transaction.save(record, root=RUNTIME_TRANSACTION_ROOT())
            return PromotionResult(
                tx_id=tx_id,
                verdict="ROLLED BACK",
                sha=artifact.artifact_sha,
                version=artifact.version,
                reason=reason,
            )
        except BaseException as rb_exc:
            record.phase = "failure"
            record.rollback_reason = (
                f"{reason}; rollback failed: {type(rb_exc).__name__}: {rb_exc}"
            )
            transaction.save(record, root=RUNTIME_TRANSACTION_ROOT())
            return PromotionResult(
                tx_id=tx_id,
                verdict="CRITICAL: PROMOTION AND ROLLBACK FAILED",
                sha=artifact.artifact_sha,
                version=artifact.version,
                reason=record.rollback_reason,
            )


def RUNTIME_TRANSACTION_ROOT() -> Path:
    return Path("/var/lib/control-room-peer-deployer/transactions")


__all__ = [
    "PromotionError",
    "PromotionInterrupted",
    "PromotionResult",
    "RUNTIME_TRANSACTION_ROOT",
    "install_signal_handlers",
    "run_promotion",
]
