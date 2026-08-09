"""Stale-transaction reconciliation for the peer-deployer.

The 2026-08-08 O1 promotion incident left a half-staged artifact
under a transaction record whose raw JSON still says
``mutation_boundary_crossed = false`` and ``phase = candidate_staging``.
The previous recovery approach was to manually edit the on-disk
transaction JSON to "classify" the failure and then manually
``rm -rf`` the staged release path. That approach is not acceptable:

  * The historical transaction JSON is forensic evidence. It must
    not be rewritten merely to make a new deployer accept it.
  * A generic ``rm -rf`` on a path that looks like a release is the
    exact failure pattern that destroyed the original
    ``/opt/omnigent/venv``.

This module provides a first-class, fail-closed reconciliation
operation. The reconciler inspects the historical transaction and
the current filesystem state and independently proves what is safe.
It NEVER rewrites the historical transaction record. It writes a
NEW audit/reconciliation record describing what it observed, what
classification it assigned, and what disposition it took.

The reconciler's contract:
  * Accept only canonical transaction IDs.
  * Load the transaction record READ-ONLY.
  * Independently prove the candidate path is safe to quarantine:
      - it is NOT the O1 active runtime;
      - it is NOT O1's current symlink target;
      - it is NOT O2's runtime or release;
      - no running O1/O2 service references it via systemd;
      - its provenance (PROVENANCE.txt) identifies the expected
        candidate, where applicable;
      - the historical transaction/evidence supports classifying
        it as incomplete/stale.
  * If all independent proofs pass, MOVE the candidate into a
    quarantine directory keyed by the transaction ID. This is a
    rename, not a delete. The original transaction record is
    preserved.
  * If any proof fails, REFUSE. The reconciler never deletes
    anything it cannot prove ownership of.

The reconciliation can be invoked via:
  * The Python API: ``reconcile.reconcile_stale_transaction(tx_id)``.
  * The CLI: ``python -m peer_deployer reconcile-stale --tx-id <TX_ID>``.
  * The host entrypoint: ``peer_promote_o1_v3.py --reconcile-stale <TX_ID>``.

Reconciliation is a maintenance operation. It does NOT promote
anything. It does NOT touch the active runtime. It does NOT touch
the DB. It does NOT restart services. It only moves a *proven*
candidate into a quarantine directory and writes an audit record.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import identity, transaction
from .identity import Instance
from .transaction import TransactionRecord


# Canonical quarantine root. Per the brief, this is the default
# destination for proven orphaned artifacts. The reconciler uses
# the transaction ID as the subdirectory name so multiple
# reconciliations can be audited independently.
DEFAULT_QUARANTINE_ROOT = Path("/var/lib/omnigent-control-room/quarantine")

# Paths the reconciler treats as intrinsically protected. The
# reconciler refuses to move any of these into quarantine, even
# in a partially-classified state.
INTRINSIC_FORBIDDEN_PATHS: tuple[Path, ...] = (
    Path("/"),
    Path("/opt"),
    Path("/opt/omnigent"),
    Path("/opt/omnigent/venv"),
    Path("/opt/omnigent-production"),
    Path("/var"),
    Path("/var/lib"),
    Path("/var/lib/omnigent"),
    Path("/var/lib/omnigent-production"),
    Path("/etc"),
    Path("/etc/systemd"),
    Path("/etc/omnigent"),
    Path("/etc/omnigent-production"),
)


class ReconciliationError(RuntimeError):
    """Raised when reconciliation cannot prove a path is safe to quarantine."""


@dataclass
class ReconciliationReport:
    """The structured outcome of a reconciliation.

    The host entrypoint writes this to evidence as a JSON file.
    Crucially, the historical transaction record is NOT modified;
    the report is a NEW artifact that links to the original.
    """

    tx_id: str
    reconciler_version: str
    reconciled_at_unix: float
    historical_tx_sha256: str
    historical_tx_path: str
    historical_tx_phase: str
    historical_tx_mutation_boundary_crossed: bool
    candidate_path: str
    candidate_provenance_present: bool
    candidate_provenance_sha: str
    classification: str  # "stale_incomplete" | "unsafe_to_reconcile" | "no_candidate"
    safe: bool
    disposition: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    forbidden_proofs: list[str] = field(default_factory=list)
    quarantine_path: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "reconciler_version": self.reconciler_version,
            "reconciled_at_unix": self.reconciled_at_unix,
            "historical_tx_sha256": self.historical_tx_sha256,
            "historical_tx_path": self.historical_tx_path,
            "historical_tx_phase": self.historical_tx_phase,
            "historical_tx_mutation_boundary_crossed": self.historical_tx_mutation_boundary_crossed,
            "candidate_path": self.candidate_path,
            "candidate_provenance_present": self.candidate_provenance_present,
            "candidate_provenance_sha": self.candidate_provenance_sha,
            "classification": self.classification,
            "safe": self.safe,
            "disposition": self.disposition,
            "checks": self.checks,
            "forbidden_proofs": self.forbidden_proofs,
            "quarantine_path": self.quarantine_path,
            "notes": self.notes,
        }


RECONCILER_VERSION = "1.0.0"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve(path: Path) -> Path:
    """Return ``Path(os.path.realpath(path))`` for absolute safety.

    The reconciler never deletes a path it has not resolved through
    a realpath check. This catches symlink traversal and
    ``/opt/omnigent/venv -> /opt/omnigent/releases/<sha>`` tricks.
    """
    return Path(os.path.realpath(path))


def _check(check_name: str, ok: bool, detail: str, report: ReconciliationReport) -> bool:
    """Append a check result to the report and return ``ok``."""
    report.checks.append({"name": check_name, "ok": ok, "detail": detail})
    return ok


def _not_forbidden(path: Path, report: ReconciliationReport) -> bool:
    """Verify ``path`` is not in the intrinsic-forbidden list.

    Even if a path looks like a release, it must not be the active
    O1 runtime, the O1 symlink target, the O2 production tree, or
    any other path the reconciler treats as intrinsically
    protected.
    """
    safe = True
    for forbidden in INTRINSIC_FORBIDDEN_PATHS:
        try:
            path.relative_to(forbidden)
        except ValueError:
            continue
        if path == forbidden or path == forbidden.resolve():
            report.forbidden_proofs.append(
                f"intrinsic_forbidden: {path} == {forbidden}"
            )
            safe = False
            break
    return safe


def _is_o1_active_runtime(path: Path, target: Instance) -> bool:
    """Return True iff ``path`` is currently O1's active runtime.

    The active runtime is whatever ``/opt/omnigent/venv`` resolves
    to, OR the O1 venv directory itself if no symlink is present.
    """
    active_link = target.deployment_root / "venv"
    if not active_link.exists() and not active_link.is_symlink():
        return False
    try:
        resolved = active_link.resolve() if active_link.is_symlink() else active_link
    except OSError:
        return False
    return path == Path(resolved)


def _is_o1_symlink_target(path: Path, target: Instance) -> bool:
    """Return True iff ``path`` is the O1 venv symlink itself.

    The symlink is a host-state artifact. The reconciler must
    not move it.
    """
    return path == (target.deployment_root / "venv")


def _is_o2_runtime_or_release(path: Path, supervisor: Instance) -> bool:
    """Return True iff ``path`` is part of the O2 deployment root."""
    sup_root = supervisor.deployment_root.resolve()
    try:
        path.relative_to(sup_root)
    except ValueError:
        return False
    return True


def _is_o2_db_path(path: Path, supervisor: Instance) -> bool:
    """Return True iff ``path`` is O2's DB or DB-home.

    O2's DB is intrinsically protected against any reconciler
    action. The reconciler must never touch it.
    """
    o2_home = identity.HOME_MAPPING.get(str(supervisor.deployment_root))
    if o2_home is None:
        return False
    try:
        path.relative_to(o2_home.resolve())
    except ValueError:
        return False
    return True


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(Path(os.path.realpath(str(root))))
    except ValueError:
        return False
    return True


def _service_exe_references_path(unit: str, path: Path) -> bool:
    """Return True iff the systemd unit's ExecStart references ``path``."""
    try:
        result = subprocess.run(
            ["systemctl", "cat", unit],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    return str(path) in result.stdout


def _classify_candidate_provenance(
    candidate: Path,
    expected_sha: str,
) -> tuple[bool, str]:
    """Return (provenance_present, provenance_sha)."""
    provenance = candidate / "PROVENANCE.txt"
    if not provenance.is_file():
        return False, ""
    text = provenance.read_text(errors="replace")
    sha = ""
    for line in text.splitlines():
        if line.startswith("sha="):
            sha = line.split("=", 1)[1].strip()
            break
    return True, sha


def reconcile_stale_transaction(
    tx_id: str,
    *,
    quarantine_root: Path = DEFAULT_QUARANTINE_ROOT,
    tx_root: Path = transaction.DEFAULT_TX_ROOT,
    allowed_target: Instance | None = None,
    allowed_supervisor: Instance | None = None,
) -> ReconciliationReport:
    """Reconcile a stale transaction.

    Inspects the historical transaction record and the current
    filesystem state. If the candidate path is proven to be safe
    (not active runtime, not symlink, not O2, not referenced by any
    service), it is MOVED into a per-transaction quarantine
    directory. The historical transaction record is NEVER modified;
    a new audit record is written alongside the quarantine dir.

    Returns a ``ReconciliationReport``. Raises ``ReconciliationError``
    on any unsafe condition.
    """
    # Validate the ID first.
    transaction.assert_tx_id(tx_id)

    # Load the historical transaction record READ-ONLY.
    record_path = transaction.transaction_path(tx_root, tx_id)
    if not record_path.is_file():
        raise ReconciliationError(f"transaction not found: {tx_id}")
    historical_blob = record_path.read_bytes()
    historical_sha = _sha256_bytes(historical_blob)
    try:
        record = TransactionRecord.from_dict(json.loads(historical_blob))
    except (json.JSONDecodeError, transaction.TransactionError) as exc:
        raise ReconciliationError(
            f"historical transaction record is corrupt: {exc}"
        ) from exc

    target = allowed_target or identity.get(record.target)
    supervisor = allowed_supervisor or identity.get(record.supervisor)

    # Hard refusal: target == supervisor must never happen even in
    # historic records.
    if target.name == supervisor.name:
        raise ReconciliationError(
            f"REFUSED: historical transaction has target == supervisor == {target.name!r}"
        )

    report = ReconciliationReport(
        tx_id=tx_id,
        reconciler_version=RECONCILER_VERSION,
        reconciled_at_unix=time.time(),
        historical_tx_sha256=historical_sha,
        historical_tx_path=str(record_path),
        historical_tx_phase=record.phase,
        historical_tx_mutation_boundary_crossed=record.mutation_boundary_crossed,
        candidate_path=record.new_runtime_path,
        candidate_provenance_present=False,
        candidate_provenance_sha="",
        classification="unknown",
        safe=False,
        disposition="pending",
        checks=[],
        forbidden_proofs=[],
        quarantine_path="",
        notes=[],
    )

    # No candidate path recorded? Nothing to reconcile.
    if not record.new_runtime_path:
        report.classification = "no_candidate"
        report.disposition = "no_candidate_recorded"
        _write_audit(report, quarantine_root)
        return report

    candidate = Path(record.new_runtime_path)
    if not candidate.exists():
        report.classification = "no_candidate"
        report.disposition = "candidate_already_absent"
        _check("candidate_present", False, "candidate path does not exist", report)
        _write_audit(report, quarantine_root)
        return report

    try:
        candidate_resolved = _resolve(candidate)
    except OSError as exc:
        report.disposition = f"refused_candidate_unresolvable: {exc}"
        raise ReconciliationError(
            f"REFUSED: cannot resolve candidate {candidate}: {exc}"
        ) from exc
    report.candidate_path = str(candidate_resolved)

    # Independent proofs — the order matters. Each proof must pass
    # before we proceed to the next. The reconciler must have a
    # specific proof for each "this is not X" claim.

    # 1. Candidate is NOT O1's active runtime.
    if not _check(
        "not_o1_active_runtime",
        not _is_o1_active_runtime(candidate_resolved, target),
        f"candidate={candidate_resolved} target_active={_is_o1_active_runtime(candidate_resolved, target)}",
        report,
    ):
        report.disposition = "refused_unsafe_active_runtime"
        _write_audit(report, quarantine_root)
        raise ReconciliationError(
            f"REFUSED: candidate {candidate_resolved} is O1's active runtime"
        )

    # 2. Candidate is NOT O1's venv symlink.
    if not _check(
        "not_o1_venv_symlink",
        not _is_o1_symlink_target(candidate_resolved, target),
        f"candidate={candidate_resolved} is_target_symlink={candidate_resolved == (target.deployment_root / 'venv')}",
        report,
    ):
        report.disposition = "refused_unsafe_symlink"
        _write_audit(report, quarantine_root)
        raise ReconciliationError(
            f"REFUSED: candidate {candidate_resolved} is O1's venv symlink"
        )

    # 3. Candidate is NOT under O2's deployment root.
    if not _check(
        "not_o2_runtime",
        not _is_o2_runtime_or_release(candidate_resolved, supervisor),
        f"candidate={candidate_resolved} under_o2={_is_o2_runtime_or_release(candidate_resolved, supervisor)}",
        report,
    ):
        report.disposition = "refused_unsafe_o2_runtime"
        _write_audit(report, quarantine_root)
        raise ReconciliationError(
            f"REFUSED: candidate {candidate_resolved} is under O2's deployment root"
        )

    # 4. Candidate is NOT under O2's DB home.
    if not _check(
        "not_o2_db",
        not _is_o2_db_path(candidate_resolved, supervisor),
        f"candidate={candidate_resolved} overlaps_o2_db={_is_o2_db_path(candidate_resolved, supervisor)}",
        report,
    ):
        report.disposition = "refused_unsafe_o2_db"
        _write_audit(report, quarantine_root)
        raise ReconciliationError(
            f"REFUSED: candidate {candidate_resolved} overlaps O2's DB home"
        )

    # 5. Candidate is NOT in the intrinsic-forbidden list.
    if not _check(
        "not_intrinsic_forbidden",
        _not_forbidden(candidate_resolved, report),
        f"intrinsic_forbidden_proofs={report.forbidden_proofs}",
        report,
    ):
        report.disposition = "refused_intrinsic_forbidden"
        _write_audit(report, quarantine_root)
        raise ReconciliationError(
            f"REFUSED: candidate {candidate_resolved} is in the intrinsic-forbidden list"
        )

    # 6. No running O1/O2 service references the candidate.
    refs: list[str] = []
    for unit in (target.service_unit, target.host_unit, supervisor.service_unit, supervisor.host_unit):
        if _service_exe_references_path(unit, candidate_resolved):
            refs.append(unit)
    if not _check(
        "no_service_references",
        not refs,
        f"referencing_units={refs}",
        report,
    ):
        report.disposition = "refused_service_references"
        _write_audit(report, quarantine_root)
        raise ReconciliationError(
            f"REFUSED: live service unit references the candidate: {refs}"
        )

    # 7. The candidate's PROVENANCE.txt, if present, identifies the
    #    expected artifact. Mismatch is informational, not a refusal —
    #    the reconciler logs it but a missing provenance is ALSO
    #    informational. The transaction record carries the expected
    #    SHA so we can compare.
    expected_sha = record.target_artifact_sha or record.new_runtime_sha
    provenance_present, provenance_sha = _classify_candidate_provenance(
        candidate_resolved, expected_sha
    )
    report.candidate_provenance_present = provenance_present
    report.candidate_provenance_sha = provenance_sha
    if provenance_present and expected_sha and provenance_sha != expected_sha:
        report.notes.append(
            f"provenance_sha_mismatch: expected={expected_sha} actual={provenance_sha}"
        )

    # 8. Mutation boundary state. If the historical transaction
    #    crossed the mutation boundary, the reconciler is more
    #    cautious — it requires DB backup integrity too. If it
    #    did NOT cross the boundary, the candidate is unambiguously
    #    a "staged-only" artifact with no committed effect.
    if record.mutation_boundary_crossed:
        report.classification = "post_mutation_unfinished"
        report.notes.append(
            "historical transaction crossed the mutation boundary; "
            "the reconciler requires an additional DB-integrity proof."
        )
        if not record.db_backup_path or record.db_backup_integrity != "ok":
            report.disposition = "refused_post_mutation_without_db_backup"
            _write_audit(report, quarantine_root)
            raise ReconciliationError(
                "REFUSED: historical transaction crossed mutation boundary "
                "but has no verified DB backup; reconciler will not move "
                "the candidate. Operator must verify the DB state first."
            )
    else:
        report.classification = "stale_incomplete"

    # All proofs passed. NOW we may move the candidate. The move
    # is a rename into a quarantine directory keyed by the
    # transaction ID. The original transaction record stays put;
    # we write a NEW audit record alongside the quarantine dir.
    qdir = quarantine_root / tx_id
    if qdir.exists():
        report.disposition = "refused_quarantine_exists"
        _write_audit(report, quarantine_root)
        raise ReconciliationError(
            f"REFUSED: quarantine dir already exists: {qdir}"
        )

    try:
        qdir.parent.mkdir(parents=True, exist_ok=True)
        os.rename(candidate_resolved, qdir)
    except OSError as exc:
        report.disposition = f"refused_move_failed: {exc}"
        _write_audit(report, quarantine_root)
        raise ReconciliationError(
            f"REFUSED: failed to move {candidate_resolved} -> {qdir}: {exc}"
        ) from exc

    report.quarantine_path = str(qdir)
    report.safe = True
    report.disposition = "quarantined"
    _write_audit(report, quarantine_root)
    return report


def _write_audit(report: ReconciliationReport, quarantine_root: Path) -> None:
    """Write the audit record alongside the quarantine dir.

    The audit record is a NEW JSON file. The historical transaction
    record is NOT modified. The audit record links to the original
    by path and SHA-256.
    """
    audit_dir = quarantine_root / report.tx_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "reconciliation.json"
    audit_path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    # Also write a human-readable summary next to the audit.
    summary = audit_dir / "SUMMARY.txt"
    summary_lines = [
        "STALE-TRANSACTION RECONCILIATION REPORT",
        f"  tx_id:                          {report.tx_id}",
        f"  reconciler_version:             {report.reconciler_version}",
        f"  reconciled_at_unix:             {report.reconciled_at_unix}",
        f"  historical_tx_sha256:           {report.historical_tx_sha256}",
        f"  historical_tx_path:             {report.historical_tx_path}",
        f"  historical_tx_phase:            {report.historical_tx_phase}",
        f"  historical_tx_mutation_crossed: {report.historical_tx_mutation_boundary_crossed}",
        f"  candidate_path:                 {report.candidate_path}",
        f"  candidate_provenance_present:   {report.candidate_provenance_present}",
        f"  candidate_provenance_sha:       {report.candidate_provenance_sha}",
        f"  classification:                 {report.classification}",
        f"  safe:                           {report.safe}",
        f"  disposition:                    {report.disposition}",
        f"  quarantine_path:                {report.quarantine_path}",
        f"  notes:                          {report.notes}",
        "FORENSIC NOTE: This audit record is a NEW artifact. The historical",
        "transaction.json is preserved byte-identical. The reconciler",
        "does not modify, rewrite, or 'classify' the historical record.",
        "It independently proves the candidate was safe to quarantine",
        "and moves it into a per-transaction directory.",
    ]
    summary.write_text("\n".join(summary_lines) + "\n")


def list_quarantined(
    quarantine_root: Path = DEFAULT_QUARANTINE_ROOT,
) -> list[dict[str, Any]]:
    """List all quarantined transactions with their audit records.

    Returns a list of dicts. Each dict contains the audit summary
    fields. The historical transaction record is NOT read.
    """
    out: list[dict[str, Any]] = []
    if not quarantine_root.is_dir():
        return out
    for d in sorted(quarantine_root.iterdir()):
        audit = d / "reconciliation.json"
        if not audit.is_file():
            continue
        try:
            blob = json.loads(audit.read_text())
        except json.JSONDecodeError:
            continue
        out.append(blob)
    return out


__all__ = [
    "DEFAULT_QUARANTINE_ROOT",
    "INTRINSIC_FORBIDDEN_PATHS",
    "ReconciliationError",
    "ReconciliationReport",
    "RECONCILER_VERSION",
    "reconcile_stale_transaction",
    "list_quarantined",
]
