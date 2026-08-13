"""Mode-aware, zero-mutation preflight for peer-supervised promotion."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import acceptance, identity, reconcile, service_state, staging, transaction
from .acceptance import CandidateAcceptance
from .identity import Instance
from .mode import DeploymentMode

MIN_FREE_BYTES = 25 * 1024 * 1024 * 1024
STORAGE_GUARD_UNIT = "mlflow-storage-guard.timer"
STORAGE_GUARD_LATCH = Path("/var/lib/mlflow-storage-guard/critical.latch")


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "fatal": self.fatal,
        }


@dataclass
class PreflightReport:
    target: str
    supervisor: str
    target_artifact_sha: str = ""
    target_artifact_version: str = ""
    mode: str = ""
    acceptance_record_path: str = ""
    acceptance_record_sha256: str = ""
    passed: bool = False
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "supervisor": self.supervisor,
            "mode": self.mode,
            "target_artifact_sha": self.target_artifact_sha,
            "target_artifact_version": self.target_artifact_version,
            "acceptance_record_path": self.acceptance_record_path,
            "acceptance_record_sha256": self.acceptance_record_sha256,
            "passed": self.passed,
            "checks": [item.to_dict() for item in self.checks],
        }


@dataclass(frozen=True)
class StorageGuardStatus:
    active: bool
    latched: bool
    detail: str = ""


StorageGuardProbe = Callable[[], StorageGuardStatus]
ReleaseValidator = Callable[..., list[str]]


class PreflightError(RuntimeError):
    pass


def _record(
    report: PreflightReport,
    name: str,
    ok: bool,
    detail: str,
    fatal: bool = True,
) -> bool:
    report.checks.append(CheckResult(name, ok, detail, fatal))
    return ok


def default_storage_guard_probe() -> StorageGuardStatus:
    """Read the host storage guard without changing its state."""
    active = service_state.is_active(STORAGE_GUARD_UNIT)
    latched = STORAGE_GUARD_LATCH.exists()
    return StorageGuardStatus(
        active=active,
        latched=latched,
        detail=f"unit={STORAGE_GUARD_UNIT} latch={STORAGE_GUARD_LATCH}",
    )


def source_release_for(
    mode: DeploymentMode,
    record: CandidateAcceptance,
    supervisor: Instance,
) -> Path:
    if mode is DeploymentMode.BOOTSTRAP_FIRST_PEER:
        return Path(record.immutable_release_root)
    return supervisor.deployment_root / "releases" / record.source_sha


def target_home_for(target: Instance) -> Path:
    try:
        return identity.HOME_MAPPING[str(target.deployment_root)]
    except KeyError as exc:
        raise PreflightError(f"unknown target deployment root: {target.deployment_root}") from exc


def check_target_distinct_from_supervisor(
    report: PreflightReport, target: Instance, supervisor: Instance
) -> bool:
    try:
        identity.require_distinct(target, supervisor)
    except identity.IdentityError as exc:
        return _record(report, "common.target_distinct_from_supervisor", False, str(exc))
    return _record(
        report,
        "common.target_distinct_from_supervisor",
        True,
        f"target={target.name} supervisor={supervisor.name}",
    )


def _check_known_unit(report: PreflightReport, name: str, unit: str) -> bool:
    try:
        known = service_state.is_known(unit)
    except service_state.ServiceStateError as exc:
        return _record(report, name, False, str(exc))
    return _record(report, name, known, unit if known else f"unknown unit: {unit}")


def check_target_service_identity(report: PreflightReport, target: Instance) -> bool:
    return _check_known_unit(report, "common.target_service_known", target.service_unit)


def check_target_host_identity(report: PreflightReport, target: Instance) -> bool:
    return _check_known_unit(report, "common.target_host_known", target.host_unit)


def check_supervisor_service_identity(report: PreflightReport, supervisor: Instance) -> bool:
    first = _check_known_unit(report, "common.supervisor_service_known", supervisor.service_unit)
    second = _check_known_unit(report, "common.supervisor_host_known", supervisor.host_unit)
    return first and second


def check_supervisor_healthy(report: PreflightReport, supervisor: Instance) -> bool:
    try:
        server = service_state.is_active(supervisor.service_unit)
        host = service_state.is_active(supervisor.host_unit)
        health = identity.http_health_ok(supervisor.health_url)
    except (service_state.ServiceStateError, identity.IdentityError) as exc:
        return _record(report, "common.supervisor_healthy", False, str(exc))
    return _record(
        report,
        "common.supervisor_healthy",
        server and host and health,
        f"server_active={server} host_active={host} health_ok={health}",
    )


def check_storage_guard(
    report: PreflightReport,
    *,
    probe: StorageGuardProbe = default_storage_guard_probe,
) -> bool:
    try:
        status = probe()
    except Exception as exc:  # noqa: BLE001 - injected probes fail closed
        return _record(report, "common.storage_guard", False, f"probe failed: {exc}")
    return _record(
        report,
        "common.storage_guard",
        status.active and not status.latched,
        f"active={status.active} latched={status.latched} {status.detail}".strip(),
    )


def check_acceptance_record(
    report: PreflightReport,
    record: CandidateAcceptance,
    path: Path,
) -> bool:
    canonical = acceptance.canonical_path(record)
    if path != canonical:
        return _record(
            report,
            "common.acceptance_record",
            False,
            f"non-canonical acceptance path: {path}; expected {canonical}",
        )
    try:
        loaded = acceptance.load(
            path,
            expected_hash=record.acceptance_record_sha256,
            require_immutable_permissions=True,
        )
    except (acceptance.AcceptanceError, json.JSONDecodeError) as exc:
        return _record(report, "common.acceptance_record", False, str(exc))
    ok = loaded == record
    return _record(
        report,
        "common.acceptance_record",
        ok,
        (
            f"schema={record.schema_version} path={path} hash={record.acceptance_record_sha256}"
            if ok
            else "acceptance record changed after loading"
        ),
    )


def check_target_db(report: PreflightReport, target: Instance) -> bool:
    try:
        path = target_home_for(target) / "chat.db"
    except PreflightError as exc:
        return _record(report, "common.target_db", False, str(exc))
    if not path.is_file():
        return _record(report, "common.target_db", False, f"missing: {path}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.Error as exc:
        return _record(report, "common.target_db", False, str(exc))
    return _record(report, "common.target_db", result == "ok", f"{path} integrity={result}")


def check_rollback_dir_writable(report: PreflightReport, target: Instance) -> bool:
    del target
    root = transaction.DEFAULT_TX_ROOT
    parent = root if root.exists() else root.parent
    ok = parent.is_dir() and os.access(parent, os.W_OK)
    return _record(
        report,
        "common.rollback_dir_writable",
        ok,
        f"transaction state parent={parent}",
    )


def check_disk_space(report: PreflightReport, target: Instance) -> bool:
    try:
        home = target_home_for(target)
        free = shutil.disk_usage(home).free
    except (PreflightError, OSError) as exc:
        return _record(report, "common.disk_space", False, str(exc))
    return _record(
        report,
        "common.disk_space",
        free >= MIN_FREE_BYTES,
        f"free={free} required={MIN_FREE_BYTES}",
    )


def check_scripts_present(report: PreflightReport, target: Instance) -> bool:
    del target
    script = Path(__file__).resolve().parents[1] / "peer_promote_o1_v3.py"
    ok = script.is_file() and os.access(script, os.R_OK)
    return _record(report, "common.host_deployer_available", ok, str(script))


def check_no_other_transaction(
    report: PreflightReport,
    *,
    target: Instance | None = None,
    supervisor: Instance | None = None,
    quarantine_root: Path | None = None,
) -> bool:
    root = transaction.DEFAULT_TX_ROOT
    if not root.is_dir():
        return _record(report, "common.no_other_transaction", True, f"absent: {root}")
    target = target or identity.O1
    supervisor = supervisor or identity.O2
    quarantine = quarantine_root or reconcile.DEFAULT_QUARANTINE_ROOT
    blockers: list[str] = []
    reconciled: list[str] = []
    for entry in sorted(root.iterdir()):
        path = entry / "transaction.json"
        if not path.is_file():
            continue
        try:
            blob = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            blockers.append(f"{entry.name}:corrupt")
            continue
        phase = str(blob.get("phase", "init"))
        if phase in {"tx_committed", "rolled_back", "failure"}:
            continue
        tx_id = str(blob.get("tx_id", entry.name))
        try:
            historical_target = identity.get(str(blob.get("target", "")))
            historical_supervisor = identity.get(str(blob.get("supervisor", "")))
            identity.require_distinct(historical_target, historical_supervisor)
            result = reconcile.validate_completed_reconciliation(
                tx_id,
                tx_root=root,
                quarantine_root=quarantine,
                allowed_target=historical_target,
                allowed_supervisor=historical_supervisor,
            )
        except Exception as exc:  # noqa: BLE001 - corrupt overlays block safely
            blockers.append(f"{tx_id}:{phase}:validator:{exc}")
            continue
        if result.is_validly_reconciled:
            reconciled.append(f"{tx_id}/{phase}")
        else:
            reasons = ";".join(result.reasons)
            blockers.append(f"{tx_id}/{phase}:{reasons}")
    if blockers:
        detail = ", ".join(blockers)
    elif reconciled:
        detail = "validly reconciled: " + ", ".join(reconciled)
    else:
        detail = "none"
    return _record(
        report,
        "common.no_other_transaction",
        not blockers,
        detail,
    )


def check_service_state_helper(
    report: PreflightReport, supervisor: Instance | None = None
) -> bool:
    supervisor = supervisor or identity.O2
    try:
        active = service_state.is_active(supervisor.service_unit)
        unknown = not service_state.is_known("omnigent-does-not-exist-test-only.service")
    except service_state.ServiceStateError as exc:
        return _record(report, "common.service_state_helper", False, str(exc))
    return _record(
        report,
        "common.service_state_helper",
        active and unknown,
        f"active={active} unknown_classified={unknown}",
    )


def check_supervisor_identity_matches(
    report: PreflightReport,
    supervisor: Instance,
    record: CandidateAcceptance,
) -> bool:
    try:
        sha = identity.installed_sha(supervisor.deployment_root)
        version = identity.installed_version(supervisor.deployment_root)
    except identity.IdentityError as exc:
        return _record(report, "peer-copy.supervisor_identity", False, str(exc))
    ok = sha == record.source_sha and version == record.package_version
    return _record(
        report,
        "peer-copy.supervisor_identity",
        ok,
        f"observed={sha}/{version} accepted={record.source_sha}/{record.package_version}",
    )


def check_candidate_release(
    report: PreflightReport,
    *,
    mode: DeploymentMode,
    record: CandidateAcceptance,
    supervisor: Instance,
    release_validator: ReleaseValidator = acceptance.verify_release,
) -> bool:
    root = source_release_for(mode, record, supervisor)
    try:
        failures = release_validator(
            record,
            root,
            enforce_bound_root=mode is DeploymentMode.BOOTSTRAP_FIRST_PEER,
        )
    except (acceptance.AcceptanceError, OSError) as exc:
        failures = [str(exc)]
    prefix = mode.value
    return _record(
        report,
        f"{prefix}.accepted_candidate",
        not failures,
        str(root) if not failures else "; ".join(failures),
    )


def check_bootstrap_candidate_not_supervisor(
    report: PreflightReport,
    record: CandidateAcceptance,
    target: Instance,
    supervisor: Instance,
) -> bool:
    root = Path(record.immutable_release_root).resolve()
    supervisor_root = supervisor.deployment_root.resolve()
    expected = (target.deployment_root / "releases" / record.source_sha).resolve()
    try:
        root.relative_to(supervisor_root)
        outside_supervisor = False
    except ValueError:
        outside_supervisor = True
    safe = outside_supervisor and root == expected
    return _record(
        report,
        "bootstrap-first-peer.immutable_target_candidate",
        safe,
        f"candidate={root} expected={expected} supervisor_root={supervisor_root}",
    )


def check_dependency_bundle_reproducible(
    report: PreflightReport,
    supervisor: Instance,
    *,
    target_release_root: Path | None = None,
) -> bool:
    """Peer-copy only: prove the peer closure can be copied offline."""
    del target_release_root
    try:
        closure = staging.capture_supervisor_closure(supervisor)
    except staging.StagingError as exc:
        return _record(report, "peer-copy.dependency_bundle", False, str(exc))
    return _record(
        report,
        "peer-copy.dependency_bundle",
        bool(closure.distributions),
        f"distributions={len(closure.distributions)}; no live index",
    )


def check_candidate_runtime_staged_and_verified(
    report: PreflightReport,
    supervisor: Instance,
    *,
    staging_root: Path,
    expected_sha: str,
    expected_version: str,
) -> bool:
    del supervisor
    failures = staging.verify_candidate_complete(staging_root)
    if not failures and not staging.candidate_identity_matches(
        staging_root, expected_sha, expected_version
    ):
        failures.append("candidate runtime identity mismatch")
    return _record(
        report,
        "peer-copy.staged_candidate",
        not failures,
        str(staging_root) if not failures else "; ".join(failures),
    )


def run_preflight(
    *,
    target: Instance,
    supervisor: Instance,
    mode: DeploymentMode | str,
    acceptance_record_path: Path,
    acceptance_record: CandidateAcceptance | None = None,
    storage_guard_probe: StorageGuardProbe = default_storage_guard_probe,
    release_validator: ReleaseValidator = acceptance.verify_release,
    staging_root: Path | None = None,
    include_candidate_gate: bool = False,
) -> PreflightReport:
    """Run common checks followed by checks specific to the explicit mode."""
    selected_mode = DeploymentMode.parse(mode)
    if acceptance_record is None:
        acceptance_record = acceptance.load(acceptance_record_path)
    record = acceptance_record
    report = PreflightReport(
        target=target.name,
        supervisor=supervisor.name,
        target_artifact_sha=record.source_sha,
        target_artifact_version=record.package_version,
        mode=selected_mode.value,
        acceptance_record_path=str(acceptance_record_path),
        acceptance_record_sha256=record.acceptance_record_sha256,
    )
    common = (
        lambda: check_target_distinct_from_supervisor(report, target, supervisor),
        lambda: check_target_service_identity(report, target),
        lambda: check_target_host_identity(report, target),
        lambda: check_supervisor_service_identity(report, supervisor),
        lambda: check_supervisor_healthy(report, supervisor),
        lambda: check_storage_guard(report, probe=storage_guard_probe),
        lambda: check_acceptance_record(report, record, acceptance_record_path),
        lambda: check_target_db(report, target),
        lambda: check_rollback_dir_writable(report, target),
        lambda: check_disk_space(report, target),
        lambda: check_scripts_present(report, target),
        lambda: check_no_other_transaction(report, target=target, supervisor=supervisor),
        lambda: check_service_state_helper(report, supervisor),
    )
    for check in common:
        check()
    if selected_mode is DeploymentMode.BOOTSTRAP_FIRST_PEER:
        check_bootstrap_candidate_not_supervisor(report, record, target, supervisor)
        check_candidate_release(
            report,
            mode=selected_mode,
            record=record,
            supervisor=supervisor,
            release_validator=release_validator,
        )
    else:
        check_supervisor_identity_matches(report, supervisor, record)
        check_candidate_release(
            report,
            mode=selected_mode,
            record=record,
            supervisor=supervisor,
            release_validator=release_validator,
        )
        check_dependency_bundle_reproducible(report, supervisor)
        if include_candidate_gate and staging_root is not None:
            check_candidate_runtime_staged_and_verified(
                report,
                supervisor,
                staging_root=staging_root,
                expected_sha=record.source_sha,
                expected_version=record.package_version,
            )
    report.passed = all(item.ok for item in report.checks)
    return report


__all__ = [
    "CheckResult",
    "DeploymentMode",
    "PreflightError",
    "PreflightReport",
    "StorageGuardStatus",
    "check_acceptance_record",
    "check_bootstrap_candidate_not_supervisor",
    "check_candidate_release",
    "check_candidate_runtime_staged_and_verified",
    "check_dependency_bundle_reproducible",
    "check_disk_space",
    "check_no_other_transaction",
    "check_rollback_dir_writable",
    "check_scripts_present",
    "check_service_state_helper",
    "check_storage_guard",
    "check_supervisor_healthy",
    "check_supervisor_identity_matches",
    "check_supervisor_service_identity",
    "check_target_db",
    "check_target_distinct_from_supervisor",
    "check_target_host_identity",
    "check_target_service_identity",
    "default_storage_guard_probe",
    "run_preflight",
    "source_release_for",
    "target_home_for",
]
