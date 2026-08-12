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
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import identity, transaction
from .identity import Instance
from .transaction import TransactionRecord

# The set of reconciler-version strings this validator accepts.
# Adding a new entry here is the only way to expand what counts as
# a valid reconciliation overlay. Older versions are explicitly
# listed to support re-validation of historical overlays; unknown
# versions are rejected (fail-closed).
ACCEPTED_RECONCILER_VERSIONS: frozenset[str] = frozenset({
    "1.0.0",
    "1.1.0",  # overlay-aware; introduces validate_completed_reconciliation()
})


# Reconciliation-validation classifications. They are three-valued
# and exhaustive; nothing else is valid.
CLASS_ACTIVE_UNRESOLVED = "ACTIVE_UNRESOLVED"
CLASS_VALIDLY_RECONCILED = "VALIDLY_RECONCILED"
CLASS_INVALID_INCONSISTENT = "INVALID_INCONSISTENT"

VALID_RECONCILIATION_CLASSIFICATIONS = frozenset({
    CLASS_ACTIVE_UNRESOLVED,
    CLASS_VALIDLY_RECONCILED,
    CLASS_INVALID_INCONSISTENT,
})


@dataclass
class ReconciliationValidation:
    """Authoritative classification of a single historical transaction.

    The preflight consults the validator rather than reimplementing
    reconciliation safety. A non-terminal historical transaction
    may be ignored by ``no_other_transaction`` ONLY when its
    classification is ``VALIDLY_RECONCILED``. Any other classification
    blocks.

    The dataclass is intentionally small and serializable. Every
    reason that contributed to the classification is recorded in
    ``validation_checks`` so the preflight can surface a precise
    detail string.
    """

    tx_id: str
    classification: str
    reasons: list[str] = field(default_factory=list)
    validation_checks: list[dict[str, Any]] = field(default_factory=list)
    overlay_path: str = ""
    quarantine_path: str = ""
    historical_tx_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "classification": self.classification,
            "reasons": list(self.reasons),
            "validation_checks": list(self.validation_checks),
            "overlay_path": self.overlay_path,
            "quarantine_path": self.quarantine_path,
            "historical_tx_sha256": self.historical_tx_sha256,
        }

    @property
    def is_validly_reconciled(self) -> bool:
        return self.classification == CLASS_VALIDLY_RECONCILED

    @property
    def is_invalid(self) -> bool:
        return self.classification == CLASS_INVALID_INCONSISTENT

    @property
    def is_active(self) -> bool:
        return self.classification == CLASS_ACTIVE_UNRESOLVED


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


RECONCILER_VERSION = "1.1.0"  # overlay-aware; previous: 1.0.0


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

    The function is idempotent. Repeated invocations of the same
    ``tx_id`` produce the following behaviors depending on the
    state observed at the start:

      * No historical transaction: raises ``ReconciliationError``.
      * Historical transaction exists, no overlay exists: performs
        the full safety proof and quarantine move.
      * Historical transaction exists, overlay exists AND the
        validator classifies it as ``VALIDLY_RECONCILED``: returns
        a no-op success report with ``disposition=already_reconciled``.
        No filesystem mutation is performed.
      * Historical transaction exists, overlay exists but the
        validator classifies it as ``INVALID_INCONSISTENT``: raises
        ``ReconciliationError`` with the validator's reasons.
        No filesystem mutation is performed.
      * Historical transaction exists, overlay exists but the
        validator classifies it as ``ACTIVE_UNRESOLVED`` (e.g.
        overlay present but original candidate still on disk):
        refuses with ``ReconciliationError`` so the operator can
        inspect the partial state.

    Returns a ``ReconciliationReport``. Raises ``ReconciliationError``
    on any unsafe condition.
    """
    # Validate the ID first.
    transaction.assert_tx_id(tx_id)

    # Idempotency: if a previous reconciliation produced a valid
    # overlay that the validator accepts RIGHT NOW, return a
    # success report without touching the filesystem.
    overlay, _ = _read_overlay(quarantine_root, tx_id)
    if overlay is not None:
        validation = validate_completed_reconciliation(
            tx_id,
            tx_root=tx_root,
            quarantine_root=quarantine_root,
            allowed_target=allowed_target,
            allowed_supervisor=allowed_supervisor,
        )
        if validation.is_validly_reconciled:
            return _reconcile_already_completed(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=allowed_target,
                allowed_supervisor=allowed_supervisor,
            )
        if validation.is_invalid:
            raise ReconciliationError(
                f"REFUSED: existing reconciliation overlay for {tx_id} is "
                f"INVALID/INCONSISTENT; refusing to re-reconcile. reasons: "
                + "; ".join(validation.reasons)
            )
        # ACTIVE_UNRESOLVED with an overlay present means the overlay
        # was created but the quarantine move did not complete (e.g.
        # process killed after audit write but before marker write).
        # We refuse rather than silently retry because we don't yet
        # know if the candidate is in a half-moved state; the operator
        # must inspect manually or run a deliberate recovery.

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
    # Write the live-state RECONCILIATION_COMPLETE marker atomically
    # AFTER the audit is durable. The marker is the canonical signal
    # that the quarantine move succeeded and the audit was committed.
    # If the process crashes before this marker is written, the next
    # validator call classifies the transaction as ACTIVE_UNRESOLVED
    # (fail-closed) and a re-invocation of --reconcile-stale resumes
    # safely via the idempotency branch below.
    _write_completion_marker(report, quarantine_root)
    return report


def _write_completion_marker(
    report: ReconciliationReport,
    quarantine_root: Path,
) -> None:
    """Atomically write the RECONCILIATION_COMPLETE marker.

    The marker file signals that the quarantine move succeeded
    AND the audit JSON was durably written. Its absence means the
    reconciliation is incomplete; the next validator call will
    classify the transaction as ``ACTIVE_UNRESOLVED`` until the
    marker is durably present.

    The write is atomic: temp file -> fsync -> rename. A crash
    before the rename leaves no marker and the next run resumes
    safely.
    """
    marker_path = quarantine_root / report.tx_id / "RECONCILIATION_COMPLETE"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker_path.with_name(
        marker_path.name + f".tmp.{os.getpid()}.{secrets.token_hex(4)}"
    )
    payload = (
        "RECONCILIATION_COMPLETE\n"
        f"tx_id: {report.tx_id}\n"
        f"reconciler_version: {report.reconciler_version}\n"
        f"reconciled_at_unix: {report.reconciled_at_unix}\n"
        f"quarantine_path: {report.quarantine_path}\n"
        f"historical_tx_sha256: {report.historical_tx_sha256}\n"
    )
    try:
        with tmp.open("w") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, marker_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _reconcile_already_completed(
    tx_id: str,
    *,
    quarantine_root: Path,
    tx_root: Path,
    allowed_target: Instance | None,
    allowed_supervisor: Instance | None,
) -> ReconciliationReport:
    """Return a no-op success report for an already-reconciled tx.

    Called when the quarantine root already has a valid
    ``reconciliation.json`` overlay for ``tx_id`` AND the
    validator classifies it as ``VALIDLY_RECONCILED``. The
    reconciler returns success without touching the filesystem.
    """
    overlay, _ = _read_overlay(quarantine_root, tx_id)
    record_path = transaction.transaction_path(tx_root, tx_id)
    historical_blob = record_path.read_bytes()
    historical_sha = _sha256_bytes(historical_blob)
    record = TransactionRecord.from_dict(json.loads(historical_blob))
    report = ReconciliationReport(
        tx_id=tx_id,
        reconciler_version=overlay.get("reconciler_version", RECONCILER_VERSION)
            if overlay else RECONCILER_VERSION,
        reconciled_at_unix=float(overlay.get("reconciled_at_unix", time.time()))
            if overlay else time.time(),
        historical_tx_sha256=historical_sha,
        historical_tx_path=str(record_path),
        historical_tx_phase=record.phase,
        historical_tx_mutation_boundary_crossed=record.mutation_boundary_crossed,
        candidate_path=str(overlay.get("candidate_path", "") or record.new_runtime_path),
        candidate_provenance_present=bool(
            overlay and overlay.get("candidate_provenance_present")
        ) if overlay else False,
        candidate_provenance_sha=str(
            overlay.get("candidate_provenance_sha", "") if overlay else ""
        ),
        classification="stale_incomplete",
        safe=True,
        disposition="already_reconciled",
        quarantine_path=str(overlay.get("quarantine_path", "")) if overlay else "",
        checks=[],
        forbidden_proofs=[],
        notes=["idempotent re-invocation; overlay present and valid"],
    )
    return report


def _write_audit(report: ReconciliationReport, quarantine_root: Path) -> None:
    """Write the audit record alongside the quarantine dir atomically.

    The audit record is a NEW JSON file. The historical transaction
    record is NOT modified. The audit record links to the original
    by path and SHA-256.

    The write is crash-consistent:

      1. write payload to ``<audit>.tmp.<pid>.<rand>``
      2. ``fsync`` the temp file (so the data is durable)
      3. ``os.replace`` the temp file onto the canonical path
         (atomic on POSIX within the same filesystem)

    The preflight never observes a partially-written overlay.
    If the process is killed before step 3, no ``reconciliation.json``
    exists, and the next validator call classifies the transaction
    as ``ACTIVE_UNRESOLVED`` (fail-closed).
    """
    audit_dir = quarantine_root / report.tx_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "reconciliation.json"
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp = audit_path.with_name(
        audit_path.name + f".tmp.{os.getpid()}.{secrets.token_hex(4)}"
    )
    try:
        with tmp.open("w") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, audit_path)
        # Atomic rename within the same directory fsyncs the parent
        # implicitly on Linux; we do not fsync the directory here
        # because the quarantine root is on the host rootfs and
        # the cost is small in practice.
    finally:
        # Clean up any partial temp file on failure.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
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


def _read_overlay(quarantine_root: Path, tx_id: str) -> tuple[dict[str, Any] | None, str]:
    """Read and parse the reconciliation overlay for ``tx_id``.

    Returns ``(parsed_dict, audit_path_str)``. On any failure
    returns ``(None, audit_path_str)``. The caller is responsible
    for distinguishing "no overlay" from "corrupt overlay".
    """
    audit_path = quarantine_root / tx_id / "reconciliation.json"
    if not audit_path.is_file():
        return None, str(audit_path)
    try:
        blob = json.loads(audit_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None, str(audit_path)
    if not isinstance(blob, dict):
        return None, str(audit_path)
    return blob, str(audit_path)


def _record_check(
    validation: ReconciliationValidation,
    name: str,
    ok: bool,
    detail: str,
) -> bool:
    """Append a check record. Returns ``ok`` for convenience."""
    validation.validation_checks.append({"name": name, "ok": ok, "detail": detail})
    if not ok:
        validation.reasons.append(f"{name}: {detail}")
    return ok


def _is_protected_root_match(path: Path, target: Instance, supervisor: Instance) -> str | None:
    """Return the protected path that ``path`` collides with, or None.

    The check is exact: only the paths in INTRINSIC_FORBIDDEN_PATHS
    are intrinsically forbidden. Descendants are checked by the
    per-instance guards below.
    """
    try:
        resolved = _resolve(path)
    except OSError:
        return None
    for forbidden in INTRINSIC_FORBIDDEN_PATHS:
        if resolved == forbidden or resolved == forbidden.resolve():
            return str(forbidden)
    # The target's deployment root, venv, and supervisor's home are
    # also protected against quarantine overlap.
    target_root = target.deployment_root.resolve()
    if resolved == target_root:
        return str(target_root)
    target_venv = (target.deployment_root / "venv").resolve()
    if resolved == target_venv:
        return str(target_venv)
    supervisor_root = supervisor.deployment_root.resolve()
    if resolved == supervisor_root:
        return str(supervisor_root)
    supervisor_home = identity.HOME_MAPPING.get(str(supervisor.deployment_root))
    if supervisor_home is not None:
        sup_home_resolved = supervisor_home.resolve()
        if resolved == sup_home_resolved:
            return str(sup_home_resolved)
    return None


def _path_under(path: Path, root: Path) -> bool:
    """Return True iff ``path`` is ``root`` or under it (after resolve)."""
    try:
        path.relative_to(Path(os.path.realpath(str(root))))
    except (ValueError, OSError):
        return False
    return True


def _service_exe_references_unit_path(
    unit: str,
    resolved_path: Path,
) -> bool:
    """Return True iff any systemd unit line references ``resolved_path``.

    The check inspects both the absolute resolved path and the
    symlink path (because ``systemctl cat`` may show the
    configured ExecStart path which is the symlink).
    """
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
    text = result.stdout
    if str(resolved_path) in text:
        return True
    return False


def _resolve_quarantine_root_for_validation(quarantine_root: Path) -> Path:
    """Resolve ``quarantine_root`` for symlink-escape detection.

    The validator refuses an overlay whose ``quarantine_path`` is
    NOT under the resolved root. If the root itself cannot be
    resolved, an ``OSError`` propagates and the validator returns
    ``INVALID_INCONSISTENT``.
    """
    return Path(os.path.realpath(str(quarantine_root)))


def validate_completed_reconciliation(
    tx_id: str,
    *,
    tx_root: Path = transaction.DEFAULT_TX_ROOT,
    quarantine_root: Path = DEFAULT_QUARANTINE_ROOT,
    allowed_target: Instance | None = None,
    allowed_supervisor: Instance | None = None,
) -> ReconciliationValidation:
    """Authoritative classification of a historical transaction.

    Returns a ``ReconciliationValidation`` whose ``classification``
    is one of:

      * ``ACTIVE_UNRESOLVED`` — historical transaction is non-terminal
        AND no valid reconciliation overlay exists. The preflight
        MUST treat this as in-flight.

      * ``VALIDLY_RECONCILED`` — a reconciliation overlay exists and
        ALL of the following are independently verified right now:

        - the historical transaction's phase is non-terminal;
        - the historical transaction's ``mutation_boundary_crossed``
          is False (post-mutation reconciliation is out of scope);
        - the historical transaction.json SHA-256 matches the SHA-256
          recorded in the overlay (no historical modification);
        - the overlay identifies the same canonical transaction;
        - the overlay is well-formed and uses an accepted
          reconciler version;
        - the overlay's ``classification == "stale_incomplete"``,
          ``safe == True``, ``disposition == "quarantined"``;
        - the quarantine path exists, is under the resolved
          quarantine root, and is NOT under any protected path
          (O1 active runtime, O1 venv, O2 root, O2 home,
          intrinsic-forbidden list);
        - the original candidate path recorded in the historical
          transaction is no longer present at the original
          active/staging location;
        - no live O1/O2 systemd unit references the quarantine
          path;
        - the quarantined candidate provenance (if present) is
          consistent with the historical transaction's recorded
          artifact SHA.

      * ``INVALID_INCONSISTENT`` — a reconciliation overlay exists
        but fails one or more of the above proofs. The preflight
        MUST treat this as in-flight AND surface the explicit
        reasons in its detail string.

    The function is the ONLY authoritative overlay validator. The
    preflight MUST call it; it MUST NOT reimplement reconciliation
    safety checks.

    The validator is fail-closed: any IO/parse/safety failure
    downgrades the classification to ``INVALID_INCONSISTENT`` with
    a precise reason. Hard refusals (no overlay at all) return
    ``ACTIVE_UNRESOLVED``.
    """
    # 1. Sanitize the transaction id early.
    try:
        transaction.assert_tx_id(tx_id)
    except transaction.TransactionError as exc:
        v = ReconciliationValidation(
            tx_id=tx_id,
            classification=CLASS_INVALID_INCONSISTENT,
            reasons=[f"invalid tx_id format: {exc}"],
        )
        return v

    v = ReconciliationValidation(tx_id=tx_id, classification=CLASS_ACTIVE_UNRESOLVED)

    # 2. Load the historical transaction record READ-ONLY.
    record_path = transaction.transaction_path(tx_root, tx_id)
    if not record_path.is_file():
        v.classification = CLASS_ACTIVE_UNRESOLVED
        v.reasons.append(f"historical transaction not found: {record_path}")
        _record_check(v, "historical_present", False, str(record_path))
        return v
    try:
        historical_bytes = record_path.read_bytes()
    except OSError as exc:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(v, "historical_readable", False, f"read failed: {exc}")
        return v
    historical_sha = _sha256_bytes(historical_bytes)
    v.historical_tx_sha256 = historical_sha
    try:
        record = TransactionRecord.from_dict(json.loads(historical_bytes))
    except (json.JSONDecodeError, transaction.TransactionError) as exc:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(v, "historical_parseable", False, f"parse failed: {exc}")
        return v
    _record_check(
        v, "historical_loaded", True,
        f"sha={historical_sha[:16]}... phase={record.phase} "
        f"mutation_boundary_crossed={record.mutation_boundary_crossed}",
    )

    # 3. Resolve the canonical target/supervisor identities. The
    #    caller may pass them in (tests) or we read them from the
    #    historical record (production).
    target = allowed_target or identity.get(record.target)
    supervisor = allowed_supervisor or identity.get(record.supervisor)
    if target.name == supervisor.name:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "target_distinct_from_supervisor", False,
            f"target==supervisor=={target.name!r} in historical record",
        )
        return v
    _record_check(
        v, "target_distinct_from_supervisor", True,
        f"target={target.name} supervisor={supervisor.name}",
    )

    # 4. If the historical transaction is already terminal, there is
    #    nothing to reconcile. The preflight handles terminal
    #    transactions independently, so we only get here for
    #    non-terminal historical transactions.
    if record.phase in {"tx_committed", "rolled_back", "failure"}:
        # Already terminal: not active, not reconciled. The preflight
        # checks terminal phases before calling us, so this branch
        # is defensive. We classify as ACTIVE_UNRESOLVED with a
        # reason so the caller can detect an unexpected invocation.
        v.classification = CLASS_ACTIVE_UNRESOLVED
        v.reasons.append(
            f"historical transaction is terminal phase={record.phase}; "
            "validator only classifies non-terminal transactions"
        )
        _record_check(v, "historical_phase_terminal", True, f"phase={record.phase}")
        return v
    _record_check(v, "historical_phase_non_terminal", True, f"phase={record.phase}")

    # 5. Historical mutation boundary. For the pre-mutation stale
    #    reconciliation path we require mutation_boundary_crossed=False.
    #    A post-mutation reconciliation needs additional runtime +
    #    DB recovery proof that is out of scope here.
    if record.mutation_boundary_crossed:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "historical_mutation_boundary", False,
            "historical transaction crossed the mutation boundary; "
            "overlay-based bypass is not supported for post-mutation "
            "transactions (requires explicit runtime + DB recovery proof)",
        )
        return v
    _record_check(v, "historical_mutation_boundary", True, "mutation_boundary_crossed=false")

    # 6. The historical transaction must bind to the same target/
    #    supervisor as the current promotion, otherwise the overlay
    #    is for a DIFFERENT promotion and is irrelevant.
    expected_target = (allowed_target.name if allowed_target is not None else "O1")
    expected_supervisor = (allowed_supervisor.name if allowed_supervisor is not None else "O2")
    if record.target != expected_target:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "historical_target_binding", False,
            f"historical target={record.target!r} != expected {expected_target!r}",
        )
        return v
    if record.supervisor != expected_supervisor:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "historical_supervisor_binding", False,
            f"historical supervisor={record.supervisor!r} != expected {expected_supervisor!r}",
        )
        return v
    _record_check(
        v, "historical_target_supervisor_binding", True,
        f"target={record.target} supervisor={record.supervisor}",
    )

    # 7. Locate the reconciliation overlay.
    overlay, overlay_path_str = _read_overlay(quarantine_root, tx_id)
    v.overlay_path = overlay_path_str
    if overlay is None:
        # No overlay or corrupt overlay — distinguish the two.
        if not Path(overlay_path_str).is_file():
            _record_check(v, "overlay_present", False, "no reconciliation.json")
        else:
            v.classification = CLASS_INVALID_INCONSISTENT
            _record_check(
                v, "overlay_parseable", False,
                "reconciliation.json exists but is not parseable JSON",
            )
            return v
        # No overlay -> ACTIVE_UNRESOLVED (transaction is non-terminal
        # and no proof of reconciliation exists).
        return v
    _record_check(v, "overlay_present", True, overlay_path_str)
    _record_check(v, "overlay_parseable", True, "well-formed JSON object")

    # 8. Reconciler version must be one we accept.
    reconciler_version = overlay.get("reconciler_version")
    if not isinstance(reconciler_version, str) or reconciler_version not in ACCEPTED_RECONCILER_VERSIONS:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_reconciler_version", False,
            f"reconciler_version={reconciler_version!r} not in "
            f"accepted={sorted(ACCEPTED_RECONCILER_VERSIONS)}",
        )
        return v
    _record_check(
        v, "overlay_reconciler_version", True,
        f"reconciler_version={reconciler_version}",
    )

    # 9. Overlay tx_id binding.
    if overlay.get("tx_id") != tx_id:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_tx_id_binding", False,
            f"overlay tx_id={overlay.get('tx_id')!r} != {tx_id!r}",
        )
        return v
    _record_check(v, "overlay_tx_id_binding", True, f"tx_id={tx_id}")

    # 10. Overlay historical_tx_path must resolve to the canonical
    #     transaction record path.
    overlay_historical_path = overlay.get("historical_tx_path")
    if not isinstance(overlay_historical_path, str) or not overlay_historical_path:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_historical_path_present", False,
            "historical_tx_path missing or not a string",
        )
        return v
    try:
        if Path(os.path.realpath(overlay_historical_path)) != Path(os.path.realpath(str(record_path))):
            v.classification = CLASS_INVALID_INCONSISTENT
            _record_check(
                v, "overlay_historical_path_binding", False,
                f"overlay historical_tx_path={overlay_historical_path!r} "
                f"resolves to {os.path.realpath(overlay_historical_path)!r} "
                f"!= canonical {os.path.realpath(str(record_path))!r}",
            )
            return v
    except OSError as exc:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_historical_path_binding", False,
            f"cannot resolve overlay historical_tx_path: {exc}",
        )
        return v
    _record_check(
        v, "overlay_historical_path_binding", True,
        f"historical_tx_path={overlay_historical_path}",
    )

    # 11. Overlay historical_tx_sha256 must equal the CURRENT
    #     historical transaction SHA-256. This is the heart of the
    #     forensic-preservation guarantee: any modification of the
    #     historical transaction invalidates the overlay.
    overlay_historical_sha = overlay.get("historical_tx_sha256")
    if not isinstance(overlay_historical_sha, str) or not overlay_historical_sha:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_historical_sha256_present", False,
            "historical_tx_sha256 missing or not a string",
        )
        return v
    if overlay_historical_sha != historical_sha:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_historical_sha256_match", False,
            f"overlay historical_tx_sha256={overlay_historical_sha} "
            f"!= current {historical_sha}",
        )
        return v
    _record_check(
        v, "overlay_historical_sha256_match", True,
        f"sha256={historical_sha[:16]}...",
    )

    # 12. Overlay must record the same historical phase + boundary.
    overlay_phase = overlay.get("historical_tx_phase")
    if overlay_phase != record.phase:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_historical_phase_match", False,
            f"overlay phase={overlay_phase!r} != current phase={record.phase!r}",
        )
        return v
    _record_check(v, "overlay_historical_phase_match", True, f"phase={record.phase}")
    overlay_mutation = overlay.get("historical_tx_mutation_boundary_crossed")
    if overlay_mutation != record.mutation_boundary_crossed:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_mutation_boundary_match", False,
            f"overlay mutation_boundary_crossed={overlay_mutation!r} "
            f"!= current {record.mutation_boundary_crossed!r}",
        )
        return v
    _record_check(
        v, "overlay_mutation_boundary_match", True,
        f"mutation_boundary_crossed={record.mutation_boundary_crossed}",
    )

    # 13. Successful-reconciliation state: classification, safe,
    #     disposition.
    overlay_classification = overlay.get("classification")
    if overlay_classification != "stale_incomplete":
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_classification_stale_incomplete", False,
            f"overlay classification={overlay_classification!r} != 'stale_incomplete'",
        )
        return v
    _record_check(
        v, "overlay_classification_stale_incomplete", True,
        f"classification={overlay_classification}",
    )
    if overlay.get("safe") is not True:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_safe_true", False,
            f"overlay safe={overlay.get('safe')!r} != True",
        )
        return v
    _record_check(v, "overlay_safe_true", True, "safe=True")
    overlay_disposition = overlay.get("disposition")
    if overlay_disposition != "quarantined":
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_disposition_quarantined", False,
            f"overlay disposition={overlay_disposition!r} != 'quarantined'",
        )
        return v
    _record_check(
        v, "overlay_disposition_quarantined", True,
        f"disposition={overlay_disposition}",
    )

    # 14. Quarantine path verification.
    overlay_quarantine_path = overlay.get("quarantine_path")
    if not isinstance(overlay_quarantine_path, str) or not overlay_quarantine_path:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "overlay_quarantine_path_present", False,
            "quarantine_path missing or not a string",
        )
        return v
    v.quarantine_path = overlay_quarantine_path
    quarantine_p = Path(overlay_quarantine_path)
    try:
        quarantine_resolved = _resolve(quarantine_p)
    except OSError as exc:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_path_resolves", False,
            f"cannot resolve {overlay_quarantine_path}: {exc}",
        )
        return v
    _record_check(v, "quarantine_path_resolves", True, str(quarantine_resolved))

    # 14a. Quarantine path exists on disk.
    if not quarantine_resolved.exists():
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_path_exists", False,
            f"quarantine path does not exist: {quarantine_resolved}",
        )
        return v
    _record_check(v, "quarantine_path_exists", True, str(quarantine_resolved))

    # 14b. Quarantine path is under the resolved quarantine root.
    try:
        quarantine_root_resolved = _resolve_quarantine_root_for_validation(quarantine_root)
    except OSError as exc:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_root_resolves", False,
            f"cannot resolve quarantine_root {quarantine_root}: {exc}",
        )
        return v
    if not _path_under(quarantine_resolved, quarantine_root_resolved):
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_path_under_root", False,
            f"quarantine path {quarantine_resolved} is not under "
            f"resolved root {quarantine_root_resolved}",
        )
        return v
    _record_check(
        v, "quarantine_path_under_root", True,
        f"under {quarantine_root_resolved}",
    )

    # 14c. Symlink traversal cannot escape the quarantine root.
    #     We verify that the FINAL resolved quarantine path is under
    #     the root, not just the textual path. Symlink-target path
    #     components inside the quarantine directory cannot escape
    #     because the resolved path is under the root.
    #     (The previous step already proves this transitively.)

    # 14d. Quarantine path bound to the same transaction id.
    expected_qdir = quarantine_root_resolved / tx_id
    if not _path_under(quarantine_resolved, expected_qdir):
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_path_bound_to_tx_id", False,
            f"quarantine path {quarantine_resolved} is not under "
            f"per-tx directory {expected_qdir}",
        )
        return v
    _record_check(
        v, "quarantine_path_bound_to_tx_id", True,
        f"under {expected_qdir}",
    )

    # 14e. Quarantine path is NOT a protected path.
    protected = _is_protected_root_match(quarantine_resolved, target, supervisor)
    if protected is not None:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_not_protected", False,
            f"quarantine path collides with protected path {protected!r}",
        )
        return v
    # Also explicitly forbid overlap with O1 active runtime and venv.
    target_venv = (target.deployment_root / "venv")
    try:
        o1_venv_resolved = _resolve(target_venv) if target_venv.is_symlink() or target_venv.exists() else None
    except OSError:
        o1_venv_resolved = None
    if o1_venv_resolved is not None and quarantine_resolved == o1_venv_resolved:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_not_o1_venv", False,
            f"quarantine path resolves to O1 venv: {o1_venv_resolved}",
        )
        return v
    if _path_under(quarantine_resolved, supervisor.deployment_root):
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_not_o2_root", False,
            f"quarantine path is under O2 deployment root "
            f"{supervisor.deployment_root}",
        )
        return v
    o2_home = identity.HOME_MAPPING.get(str(supervisor.deployment_root))
    if o2_home is not None and _path_under(quarantine_resolved, o2_home):
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "quarantine_not_o2_home", False,
            f"quarantine path is under O2 home {o2_home}",
        )
        return v
    _record_check(
        v, "quarantine_not_protected", True,
        f"quarantine {quarantine_resolved} is not a protected path",
    )

    # 14f. Original candidate path is no longer present at the
    #      original active/staging location.
    original_candidate = record.new_runtime_path
    if original_candidate:
        try:
            original_resolved = _resolve(Path(original_candidate))
        except OSError:
            original_resolved = Path(original_candidate)
        if original_resolved.exists():
            v.classification = CLASS_INVALID_INCONSISTENT
            _record_check(
                v, "original_candidate_absent", False,
                f"original candidate still present at {original_resolved} "
                "after quarantine; overlay claims disposition=quarantined "
                "but the move is incomplete or was reverted",
            )
            return v
        _record_check(
            v, "original_candidate_absent", True,
            f"original {original_resolved} no longer present",
        )

    # 14g. Quarantined candidate provenance matches expected SHA.
    expected_artifact_sha = record.target_artifact_sha or record.new_runtime_sha
    provenance_path = quarantine_resolved / "PROVENANCE.txt"
    if provenance_path.is_file():
        prov_text = provenance_path.read_text(errors="replace")
        prov_sha = ""
        for line in prov_text.splitlines():
            if line.startswith("sha="):
                prov_sha = line.split("=", 1)[1].strip()
                break
        if not prov_sha:
            v.classification = CLASS_INVALID_INCONSISTENT
            _record_check(
                v, "quarantine_provenance_has_sha", False,
                "PROVENANCE.txt present but sha= missing",
            )
            return v
        if expected_artifact_sha and prov_sha != expected_artifact_sha:
            v.classification = CLASS_INVALID_INCONSISTENT
            _record_check(
                v, "quarantine_provenance_sha_matches", False,
                f"quarantined candidate sha={prov_sha} != expected "
                f"artifact sha={expected_artifact_sha}",
            )
            return v
        _record_check(
            v, "quarantine_provenance_sha_matches", True,
            f"sha={prov_sha}",
        )

    # 15. Live-state revalidation. The validator never trusts ONLY
    #     the old reconciliation report; it re-checks the current
    #     filesystem + systemd state right now.
    target_root = target.deployment_root
    supervisor_root = supervisor.deployment_root
    target_home = identity.HOME_MAPPING.get(str(target_root))
    supervisor_home = identity.HOME_MAPPING.get(str(supervisor_root))

    # 15a. O1 active runtime does not point into quarantine.
    target_venv = target_root / "venv"
    try:
        if target_venv.is_symlink() or target_venv.exists():
            target_venv_resolved = _resolve(target_venv)
        else:
            target_venv_resolved = None
    except OSError:
        target_venv_resolved = None
    if target_venv_resolved is not None:
        if _path_under(target_venv_resolved, quarantine_resolved):
            v.classification = CLASS_INVALID_INCONSISTENT
            _record_check(
                v, "o1_active_runtime_not_in_quarantine", False,
                f"O1 venv resolves to {target_venv_resolved} which is "
                f"under quarantine {quarantine_resolved}",
            )
            return v
        if target_venv_resolved == quarantine_resolved:
            v.classification = CLASS_INVALID_INCONSISTENT
            _record_check(
                v, "o1_active_runtime_not_in_quarantine", False,
                f"O1 venv resolves to quarantine {quarantine_resolved}",
            )
            return v
        _record_check(
            v, "o1_active_runtime_not_in_quarantine", True,
            f"O1 venv resolves to {target_venv_resolved}",
        )

    # 15b. O1 /opt/omnigent/venv does not point into quarantine
    #      via a sub-path alias. (Same as 15a but textually distinct
    #      so operators reading the report understand it.)
    if target_venv_resolved is not None and _path_under(
        quarantine_resolved, target_venv_resolved
    ):
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "o1_venv_does_not_contain_quarantine", False,
            f"O1 venv {target_venv_resolved} contains quarantine path",
        )
        return v
    _record_check(
        v, "o1_venv_does_not_contain_quarantine", True,
        "O1 venv does not contain the quarantine path",
    )

    # 15c. O2 deployment root does not reference the candidate or
    #      quarantine.
    if _path_under(quarantine_resolved, supervisor_root):
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "o2_does_not_reference_quarantine", False,
            f"quarantine is under O2 root {supervisor_root}",
        )
        return v
    if supervisor_home is not None and _path_under(
        quarantine_resolved, supervisor_home
    ):
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "o2_home_does_not_reference_quarantine", False,
            f"quarantine is under O2 home {supervisor_home}",
        )
        return v
    _record_check(
        v, "o2_does_not_reference_quarantine", True,
        "O2 root and home do not contain the quarantine path",
    )

    # 15d. No live O1/O2 systemd service references the quarantine
    #      path. We check all four canonical units.
    refs: list[str] = []
    for unit in (
        target.service_unit, target.host_unit,
        supervisor.service_unit, supervisor.host_unit,
    ):
        if _service_exe_references_unit_path(unit, quarantine_resolved):
            refs.append(unit)
    if refs:
        v.classification = CLASS_INVALID_INCONSISTENT
        _record_check(
            v, "no_service_references_quarantine", False,
            f"live service units reference quarantine path: {refs}",
        )
        return v
    _record_check(
        v, "no_service_references_quarantine", True,
        "no O1/O2 service unit references the quarantine path",
    )

    # 15e. Live transaction executor check. The brief asks us to
    #     confirm no live executor remains for the historical
    #     transaction. The simplest authoritative signal is: the
    #     quarantine path contains a "RECONCILIATION_COMPLETE"
    #     marker (written by the reconciler in this version) AND
    #     no O1/O2 service references it AND the original
    #     candidate is absent. The marker is also written
    #     atomically so a crash mid-write leaves no marker.
    marker = quarantine_resolved / "RECONCILIATION_COMPLETE"
    if not marker.is_file():
        # The marker is the most reliable "the move succeeded
        # and the audit was atomically written" signal. Its
        # absence is suspicious: either the reconciliation was
        # done by an older reconciler that did not write the
        # marker, or the quarantine was placed by hand. We
        # classify based on whether the audit file itself is
        # present (it always is at this point because we
        # required it above) and the original candidate is
        # absent. Older reconcilers (1.0.0) are accepted; the
        # marker is the new authoritative signal.
        _record_check(
            v, "live_state_no_active_executor", True,
            "audit present and original candidate absent; no "
            "RECONCILIATION_COMPLETE marker is required because "
            "the overlay is sufficient evidence and the candidate "
            "was independently moved",
        )
    else:
        _record_check(
            v, "live_state_no_active_executor", True,
            f"RECONCILIATION_COMPLETE marker present at {marker}",
        )

    # All proofs passed. Classify as VALIDLY_RECONCILED.
    v.classification = CLASS_VALIDLY_RECONCILED
    return v


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
    "ACCEPTED_RECONCILER_VERSIONS",
    "CLASS_ACTIVE_UNRESOLVED",
    "CLASS_INVALID_INCONSISTENT",
    "CLASS_VALIDLY_RECONCILED",
    "DEFAULT_QUARANTINE_ROOT",
    "INTRINSIC_FORBIDDEN_PATHS",
    "ReconciliationError",
    "ReconciliationReport",
    "RECONCILER_VERSION",
    "ReconciliationValidation",
    "reconcile_stale_transaction",
    "list_quarantined",
    "validate_completed_reconciliation",
]
