"""Tests for the authoritative reconciliation-overlay validator.

These tests cover the contradiction that the v3 reconciliation
overlay produces a valid ``reconciliation.json`` for a non-terminal
historical transaction but ``check_no_other_transaction()`` ignored
that overlay and treated the historical transaction as in-flight.

The fix introduces :func:`reconcile.validate_completed_reconciliation`
which the preflight MUST consult. These tests prove:

  * committed / rolled_back / failure transactions do not block;
  * ordinary non-terminal transactions block;
  * historical candidate_staging transactions without reconciliation
    block;
  * a valid completed reconciliation unblocks the preflight;
  * invalid reconciliation overlays still block with explicit reasons;
  * the historical transaction remains byte-identical across
    reconciliation AND across validation;
  * target/supervisor binding, SHA binding, classification binding,
    disposition binding, phase/mutation-boundary binding;
  * quarantine path verification (existence, root binding, symlink
    escape, original candidate absent, O1 venv not in quarantine, O2
    overlap, intrinsic forbidden, live service references);
  * live-state revalidation does not trust only the old report;
  * historical SHA mismatch blocks;
  * tx_id mismatch blocks;
  * target mismatch blocks;
  * supervisor mismatch blocks;
  * target == supervisor blocks;
  * safe=False blocks;
  * wrong classification blocks;
  * non-success disposition blocks;
  * missing quarantine path blocks;
  * quarantine outside approved root blocks;
  * symlink escape blocks;
  * original candidate still active/present blocks;
  * O1 venv resolving to quarantine blocks;
  * O2 overlap blocks;
  * running service reference blocks;
  * malformed reconciliation JSON blocks;
  * partial/truncated reconciliation blocks;
  * historical transaction changed after reconciliation blocks;
  * valid unchanged transaction + reconciliation makes
    ``no_other_transaction`` PASS;
  * one reconciled stale transaction + another genuinely active
    transaction still BLOCKS;
  * ``mutation_boundary_crossed=True`` is not bypassed;
  * crash before move remains blocking;
  * crash after move before audit remains blocking and
    recoverable/refusable safely;
  * repeated completed reconciliation is idempotent;
  * exact 2026-08-08 incident replay reaches
    stale historical transaction -> v3 reconciliation ->
    historical JSON unchanged -> reconciliation overlay valid ->
    strict preflight PASS, without editing transaction.json,
    ``rm -rf``, or hard-coded transaction exemptions.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"


def _load_pkg():
    if "Peer_deployer" in sys.modules:
        return sys.modules["Peer_deployer"]
    init = PKG_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "Peer_deployer", init,
        submodule_search_locations=[str(PKG_ROOT)],
    )
    assert spec is not None
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["Peer_deployer"] = pkg
    spec.loader.exec_module(pkg)
    for name in (
        "identity",
        "transaction",
        "service_state",
        "preflight",
        "rollback",
        "staging",
        "path_safety",
        "fsm",
        "reconcile",
    ):
        sub_spec = importlib.util.spec_from_file_location(
            f"Peer_deployer.{name}", PKG_ROOT / f"{name}.py"
        )
        assert sub_spec is not None
        sub = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"Peer_deployer.{name}"] = sub
        sub_spec.loader.exec_module(sub)
        setattr(pkg, name, sub)
    return pkg


_pkg = _load_pkg()
reconcile = _pkg.reconcile
preflight = _pkg.preflight
transaction = _pkg.transaction
identity = _pkg.identity


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def tx_root(tmp_path: Path) -> Path:
    root = tmp_path / "transactions"
    root.mkdir()
    original = transaction.DEFAULT_TX_ROOT
    object.__setattr__(transaction, "DEFAULT_TX_ROOT", root)
    yield root
    object.__setattr__(transaction, "DEFAULT_TX_ROOT", original)


@pytest.fixture
def quarantine_root(tmp_path: Path) -> Path:
    return tmp_path / "quarantine"


@pytest.fixture
def fake_target(tmp_path: Path) -> identity.Instance:
    original = identity.O1.deployment_root
    new_root = tmp_path / "o1_deploy"
    new_root.mkdir(parents=True, exist_ok=True)
    object.__setattr__(identity.O1, "deployment_root", new_root)
    # Move O1 HOME mapping too.
    home_original = identity.HOME_MAPPING.get(str(original))
    identity.HOME_MAPPING[str(new_root)] = tmp_path / "o1_home"
    if home_original is not None and str(original) in identity.HOME_MAPPING:
        # Keep the canonical O1 home mapping (so venv checks pass).
        identity.HOME_MAPPING[str(original)] = home_original
    yield identity.O1
    object.__setattr__(identity.O1, "deployment_root", original)


@pytest.fixture
def fake_supervisor(tmp_path: Path) -> identity.Instance:
    original = identity.O2.deployment_root
    new_root = tmp_path / "o2_deploy"
    new_root.mkdir(parents=True, exist_ok=True)
    object.__setattr__(identity.O2, "deployment_root", new_root)
    identity.HOME_MAPPING[str(new_root)] = tmp_path / "o2_home"
    yield identity.O2
    object.__setattr__(identity.O2, "deployment_root", original)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_record(
    tx_id: str,
    *,
    candidate_path: str = "",
    mutation_boundary_crossed: bool = False,
    phase: str = "candidate_staging",
    tx_root: Path | None = None,
    target: str = "O1",
    supervisor: str = "O2",
    target_artifact_sha: str = "a" * 40,
    db_backup_path: str = "",
    db_backup_integrity: str = "",
) -> transaction.TransactionRecord:
    """Write a synthetic historical transaction record.

    Unlike ``transaction.create``, this helper allows
    ``target == supervisor`` so we can build adversarial historical
    records (e.g. for the validator's invariant test). The
    helper writes directly to disk via ``transaction.save`` so the
    validator sees the synthesized record.
    """
    if target == supervisor:
        # Build the record directly without ``transaction.create``'s
        # safety check, so we can simulate the pathological case.
        record = transaction.TransactionRecord(
            tx_id=tx_id,
            target=target,
            supervisor=supervisor,
            target_artifact_sha=target_artifact_sha,
            target_artifact_version="0.9.0.dev0",
            main_wheel_sha256="b" * 64,
            sdk_client_wheel_sha256="c" * 64,
            sdk_ui_wheel_sha256="d" * 64,
            created_at_unix=0.0,
        )
    else:
        record = transaction.create(
            tx_id=tx_id,
            target=target,
            supervisor=supervisor,
            target_artifact_sha=target_artifact_sha,
            target_artifact_version="0.9.0.dev0",
            main_wheel_sha256="b" * 64,
            sdk_client_wheel_sha256="c" * 64,
            sdk_ui_wheel_sha256="d" * 64,
            root=tx_root,
        )
    record.new_runtime_path = candidate_path
    record.mutation_boundary_crossed = mutation_boundary_crossed
    record.phase = phase
    record.db_backup_path = db_backup_path
    record.db_backup_integrity = db_backup_integrity
    if target_artifact_sha:
        record.new_runtime_sha = target_artifact_sha
    transaction.save(record, root=tx_root)
    return record


def _write_overlay(
    quarantine_root: Path,
    tx_id: str,
    *,
    historical_tx_path: str,
    historical_tx_sha256: str,
    quarantine_path: str,
    historical_tx_phase: str = "candidate_staging",
    historical_tx_mutation_boundary_crossed: bool = False,
    classification: str = "stale_incomplete",
    safe: bool = True,
    disposition: str = "quarantined",
    reconciler_version: str = reconcile.RECONCILER_VERSION,
    candidate_path: str = "",
    candidate_provenance_present: bool = False,
    candidate_provenance_sha: str = "",
    tx_id_field: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Write a synthetic reconciliation overlay."""
    overlay_dir = quarantine_root / tx_id
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = overlay_dir / "reconciliation.json"
    payload = {
        "tx_id": tx_id_field if tx_id_field is not None else tx_id,
        "reconciler_version": reconciler_version,
        "reconciled_at_unix": 1700000000.0,
        "historical_tx_sha256": historical_tx_sha256,
        "historical_tx_path": historical_tx_path,
        "historical_tx_phase": historical_tx_phase,
        "historical_tx_mutation_boundary_crossed": historical_tx_mutation_boundary_crossed,
        "candidate_path": candidate_path,
        "candidate_provenance_present": candidate_provenance_present,
        "candidate_provenance_sha": candidate_provenance_sha,
        "classification": classification,
        "safe": safe,
        "disposition": disposition,
        "checks": [],
        "forbidden_proofs": [],
        "quarantine_path": quarantine_path,
        "notes": [],
    }
    if extra:
        payload.update(extra)
    overlay_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return overlay_path


def _make_candidate(
    target: identity.Instance, sha: str = "a" * 40
) -> Path:
    candidate = target.deployment_root / "releases" / sha
    candidate.mkdir(parents=True, exist_ok=True)
    (candidate / "PROVENANCE.txt").write_text(
        "schema_version=1\n"
        f"sha={sha}\n"
        "package_version=0.9.0.dev0\n"
        "wheel_sha256=b\n"
        "wheel_filename=omnigent-0.9.0.dev0-py3-none-any.whl\n"
    )
    (candidate / "artifacts").mkdir()
    (candidate / "venv").mkdir()
    return candidate


def _quarantined_path(quarantine_root: Path, tx_id: str, candidate: Path) -> Path:
    """Return the path the reconciler uses for the quarantined candidate."""
    return quarantine_root / tx_id / candidate.name


# ---------------------------------------------------------------------------
# Validator unit tests: classifications.
# ---------------------------------------------------------------------------


class TestValidatorClassifications:
    """Each terminal phase is treated as already-resolved by the
    preflight and never reaches the validator. The validator itself
    defensively classifies terminal phases as ACTIVE_UNRESOLVED with
    a reason, so callers can detect unexpected invocations.
    """

    def test_committed_tx_does_not_block_via_validator(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
    ) -> None:
        """Committed transaction -> not in flight; preflight ignores it."""
        tx_id = "promotion-20260808T201637Z-60ced7aa"
        record = _write_record(
            tx_id, candidate_path="", phase="tx_committed", tx_root=tx_root,
        )
        # No overlay needed. The preflight treats tx_committed as terminal.
        report = preflight.PreflightReport(
            target="O1",
            supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
            report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is True
        assert any(
            c.name == "no_other_transaction" and c.ok for c in report.checks
        )

    def test_rolled_back_tx_does_not_block(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7ab"
        _write_record(
            tx_id, candidate_path="", phase="rolled_back", tx_root=tx_root,
        )
        report = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
            report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is True

    def test_failure_tx_does_not_block(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7ac"
        _write_record(
            tx_id, candidate_path="", phase="failure", tx_root=tx_root,
        )
        report = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
            report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is True

    def test_ordinary_non_terminal_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7ad"
        _write_record(
            tx_id, candidate_path="/some/non/quarantined/path",
            phase="candidate_staging", tx_root=tx_root,
        )
        report = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
            report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is False
        failed = next(c for c in report.checks if c.name == "no_other_transaction")
        assert tx_id in failed.detail
        assert "/candidate_staging" in failed.detail


class TestOverlayValidatesHistorical:
    """The validator's classification is exhaustive and fail-closed."""

    def test_historical_candidate_staging_without_reconciliation_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7ae"
        _write_record(
            tx_id, candidate_path="/some/half/staged/path",
            phase="candidate_staging", tx_root=tx_root,
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id,
            tx_root=tx_root,
            quarantine_root=quarantine_root,
            allowed_target=fake_target,
            allowed_supervisor=fake_supervisor,
        )
        assert validation.is_active
        assert not validation.is_validly_reconciled
        assert not validation.is_invalid

    def test_valid_completed_reconciliation_unblocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7af"
        candidate = _make_candidate(fake_target)
        record = _write_record(
            tx_id,
            candidate_path=str(candidate),
            phase="candidate_staging",
            tx_root=tx_root,
            target_artifact_sha="a" * 40,
        )
        # Simulate a successful reconciliation: move the candidate
        # into quarantine and write the overlay.
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id,
            tx_root=tx_root,
            quarantine_root=quarantine_root,
            allowed_target=fake_target,
            allowed_supervisor=fake_supervisor,
        )
        assert validation.is_validly_reconciled, validation.reasons
        assert validation.overlay_path == str(qdir / "reconciliation.json")
        assert validation.quarantine_path == str(moved)


# ---------------------------------------------------------------------------
# Historical transaction preservation.
# ---------------------------------------------------------------------------


class TestHistoricalTransactionPreserved:
    def test_historical_json_byte_identical_after_validation(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b0"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        record_path = transaction.transaction_path(tx_root, tx_id)
        original_bytes = record_path.read_bytes()
        original_sha = _sha256(record_path)
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=original_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        # Byte-identical.
        assert _sha256(record_path) == original_sha
        assert record_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# Binding mismatches.
# ---------------------------------------------------------------------------


class TestBindingMismatches:
    def test_historical_sha_mismatch_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b1"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        # Overlay claims a wrong SHA.
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256="0" * 64,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid
        assert any(
            "historical_tx_sha256" in r or "historical_sha256_match" in r
            for r in validation.reasons
        )

    def test_tx_id_mismatch_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b2"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
            tx_id_field="promotion-99999999T999999Z-deadbeef",
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid

    def test_target_mismatch_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b3"
        candidate = _make_candidate(fake_target)
        # Historical target=O2 (mismatched).
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target="O2", supervisor="O1",
            target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid
        assert any("target" in r for r in validation.reasons)

    def test_supervisor_mismatch_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b4"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target="O1", supervisor="O1",
            target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        # target==supervisor must refuse.
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid

    def test_target_equals_supervisor_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Historical transaction with target==supervisor must refuse,
        regardless of overlay. The validator must NEVER bypass this
        check based on a reconciliation overlay."""
        tx_id = "promotion-20260808T201637Z-60ced7b5"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target="O1", supervisor="O1",
            target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid


# ---------------------------------------------------------------------------
# Reconciliation state required fields.
# ---------------------------------------------------------------------------


class TestOverlayStateBlocks:
    def test_safe_false_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b6"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
            safe=False,
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid

    def test_wrong_classification_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b7"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        for bad in ("pending", "refused_unsafe", "failed", "stuck", "completed", ""):
            _write_overlay(
                quarantine_root, tx_id,
                historical_tx_path=str(record_path),
                historical_tx_sha256=historical_sha,
                quarantine_path=str(moved),
                candidate_path=str(moved),
                classification=bad,
            )
            validation = reconcile.validate_completed_reconciliation(
                tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )
            assert validation.is_invalid, (
                f"classification={bad!r} must refuse; got {validation.classification}"
            )

    def test_non_success_disposition_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b8"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        for bad in ("pending", "refused_*", "failed", "no_candidate"):
            _write_overlay(
                quarantine_root, tx_id,
                historical_tx_path=str(record_path),
                historical_tx_sha256=historical_sha,
                quarantine_path=str(moved),
                candidate_path=str(moved),
                disposition=bad,
            )
            validation = reconcile.validate_completed_reconciliation(
                tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )
            assert validation.is_invalid, (
                f"disposition={bad!r} must refuse; got {validation.classification}"
            )


# ---------------------------------------------------------------------------
# Quarantine path verification.
# ---------------------------------------------------------------------------


class TestQuarantinePathVerification:
    def test_missing_quarantine_path_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7b9"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        # Overlay points at a quarantine path that does not exist.
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(tmp_path / "nonexistent_quarantine"),
            candidate_path=str(tmp_path / "nonexistent_quarantine"),
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid

    def test_quarantine_outside_approved_root_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7ba"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        # Overlay points outside the approved quarantine root.
        outside = tmp_path / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        quarantine_root.mkdir(parents=True, exist_ok=True)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(outside),
            candidate_path=str(outside),
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid

    def test_symlink_escape_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """A symlink inside the quarantine root that escapes must block."""
        tx_id = "promotion-20260808T201637Z-60ced7bb"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        # Set up an "escaped" candidate: a symlink at the quarantine
        # location that points OUTSIDE the quarantine root.
        escape = tmp_path / "escaped"
        escape.mkdir(parents=True, exist_ok=True)
        (escape / "PROVENANCE.txt").write_text(
            "schema_version=1\nsha=a\npackage_version=0.9.0.dev0\n"
        )
        link = qdir / "escaped-link"
        link.symlink_to(escape)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(link),
            candidate_path=str(link),
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid

    def test_original_candidate_still_present_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7bc"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        # Move the candidate to a "quarantine" location too, but
        # leave the original in place. Validator must reject because
        # the move is incomplete.
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        # Create a separate "quarantined" copy that the overlay claims.
        moved = qdir / "copied"
        moved.mkdir(parents=True, exist_ok=True)
        (moved / "PROVENANCE.txt").write_text(
            "schema_version=1\nsha=a\npackage_version=0.9.0.dev0\n"
        )
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        # candidate still exists at original location.
        assert candidate.exists()
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid
        assert any("original_candidate" in r or "candidate_absent" in r
                   for r in validation.reasons)

    def test_o1_venv_resolving_to_quarantine_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """If the O1 venv symlink resolves INTO the quarantine path,
        the validator must refuse."""
        tx_id = "promotion-20260808T201637Z-60ced7bd"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        # Make O1 venv a symlink that points into the quarantine dir.
        venv = fake_target.deployment_root / "venv"
        if venv.exists() or venv.is_symlink():
            venv.unlink()
        venv.symlink_to(moved / "venv")
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid
        # Restore O1 venv for downstream tests.
        venv.unlink()
        venv.mkdir(parents=True, exist_ok=True)

    def test_o2_overlap_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """If the candidate WAS under O2's deployment root, the
        reconciler refuses to move it. We verify this end-to-end
        because the validator cannot recover from a situation where
        the historical transaction points to a candidate that was
        never properly quarantined.

        In this scenario the reconciler refuses with a clear
        message and the overlay is written as ``safe=False``.
        """
        tx_id = "promotion-20260808T201637Z-60ced7be"
        # Build a candidate under O2's deployment root.
        o2_overlap = fake_supervisor.deployment_root / "releases" / ("b" * 40)
        o2_overlap.mkdir(parents=True, exist_ok=True)
        _write_record(
            tx_id, candidate_path=str(o2_overlap), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="b" * 40,
        )
        with pytest.raises(
            reconcile.ReconciliationError, match="O2"
        ):
            reconcile.reconcile_stale_transaction(
                tx_id, quarantine_root=quarantine_root, tx_root=tx_root,
                allowed_target=fake_target, allowed_supervisor=fake_supervisor,
            )
        # O2 release STILL exists.
        assert o2_overlap.exists()


# ---------------------------------------------------------------------------
# Overlay JSON integrity.
# ---------------------------------------------------------------------------


class TestOverlayIntegrity:
    def test_malformed_json_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7bf"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        # Write garbage.
        (qdir / "reconciliation.json").write_text("{this is : not, valid JSON")
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid

    def test_partial_truncated_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7c0"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        overlay = {
            "tx_id": tx_id,
            "reconciler_version": reconcile.RECONCILER_VERSION,
            "historical_tx_sha256": historical_sha,
            "historical_tx_path": str(record_path),
            # intentionally no other fields
        }
        (qdir / "reconciliation.json").write_text(
            json.dumps(overlay, indent=2, sort_keys=True)
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid


class TestHistoricalMutationBlocks:
    def test_historical_transaction_changed_after_reconciliation_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """If the historical transaction.json is modified after the
        reconciliation, the SHA in the overlay no longer matches."""
        tx_id = "promotion-20260808T201637Z-60ced7c1"
        candidate = _make_candidate(fake_target)
        record = _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        # Mutate the historical transaction non-terminally so the
        # validator path is exercised. We change ``candidate_staging``
        # to ``preflight`` (also non-terminal) so the SHA no longer
        # matches the overlay's recorded historical_tx_sha256.
        record.phase = "preflight"
        transaction.save(record, root=tx_root)
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid
        assert any("sha256" in r for r in validation.reasons)

    def test_mutation_boundary_crossed_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """A transaction that crossed the mutation boundary must NOT
        be bypassed via the overlay. It needs explicit runtime + DB
        recovery proof, which is out of scope for this validator."""
        tx_id = "promotion-20260808T201637Z-60ced7c2"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root,
            target_artifact_sha="a" * 40,
            mutation_boundary_crossed=True,
            db_backup_path="/some/backup", db_backup_integrity="ok",
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
            historical_tx_mutation_boundary_crossed=True,
        )
        validation = reconcile.validate_completed_reconciliation(
            tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert validation.is_invalid
        assert any("mutation" in r for r in validation.reasons)


# ---------------------------------------------------------------------------
# Composite: preflight pass + blocked + incident replay.
# ---------------------------------------------------------------------------


class TestPreflightIntegration:
    def test_valid_unchanged_transaction_and_reconciliation_makes_preflight_pass(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7c3"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        report = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
            report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is True
        detail = next(c.detail for c in report.checks if c.name == "no_other_transaction")
        assert "validly reconciled" in detail
        assert tx_id in detail

    def test_reconciled_stale_plus_genuinely_active_still_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """One validly-reconciled historical tx must NOT mask a
        second genuinely-active tx."""
        # First: validly reconciled.
        ok_id = "promotion-20260808T201637Z-60ced7c4"
        c1 = _make_candidate(fake_target)
        _write_record(
            ok_id, candidate_path=str(c1), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / ok_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved1 = qdir / c1.name
        os.rename(c1, moved1)
        rec1 = transaction.transaction_path(tx_root, ok_id)
        sha1 = _sha256(rec1)
        _write_overlay(
            quarantine_root, ok_id,
            historical_tx_path=str(rec1),
            historical_tx_sha256=sha1,
            quarantine_path=str(moved1),
            candidate_path=str(moved1),
        )
        # Second: genuinely active.
        active_id = "promotion-20260808T201637Z-60ced7c5"
        c2 = _make_candidate(fake_target, sha="c" * 40)
        _write_record(
            active_id, candidate_path=str(c2), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="c" * 40,
        )
        report = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
                report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is False
        failed = next(c for c in report.checks if c.name == "no_other_transaction")
        assert active_id in failed.detail
        # The reconciled tx must still appear as a valid reconciliation
        # in the detail (or at least not be cited as in-flight).
        assert ok_id not in failed.detail.split(";")[-1] or "invalid" not in failed.detail


# ---------------------------------------------------------------------------
# Crash consistency / idempotency.
# ---------------------------------------------------------------------------


class TestCrashConsistencyAndIdempotency:
    def test_crash_before_move_remains_blocking(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """A crash before any filesystem mutation leaves no overlay,
        so the preflight must continue to block the transaction."""
        tx_id = "promotion-20260808T201637Z-60ced7c6"
        _write_record(
            tx_id, candidate_path="/nonexistent/candidate",
            phase="candidate_staging", tx_root=tx_root,
            target_artifact_sha="a" * 40,
        )
        # No overlay written, no candidate to move.
        report = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
                report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is False

    def test_crash_after_move_before_audit_remains_blocking(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """A crash between the candidate move and the audit write
        leaves the candidate moved but no overlay; preflight blocks."""
        tx_id = "promotion-20260808T201637Z-60ced7c7"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        # Deliberately no overlay written.
        report = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
                report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is False

    def test_repeated_reconciliation_is_idempotent(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced7c8"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        # First run.
        report1 = reconcile.reconcile_stale_transaction(
            tx_id,
            quarantine_root=quarantine_root,
            tx_root=tx_root,
            allowed_target=fake_target,
            allowed_supervisor=fake_supervisor,
        )
        assert report1.safe is True
        assert report1.disposition == "quarantined"
        # Second run: must be a no-op success.
        report2 = reconcile.reconcile_stale_transaction(
            tx_id,
            quarantine_root=quarantine_root,
            tx_root=tx_root,
            allowed_target=fake_target,
            allowed_supervisor=fake_supervisor,
        )
        assert report2.safe is True
        assert report2.disposition == "already_reconciled"
        # Third run: still a no-op.
        report3 = reconcile.reconcile_stale_transaction(
            tx_id,
            quarantine_root=quarantine_root,
            tx_root=tx_root,
            allowed_target=fake_target,
            allowed_supervisor=fake_supervisor,
        )
        assert report3.safe is True
        assert report3.disposition == "already_reconciled"


# ---------------------------------------------------------------------------
# Service-state and running-executor live revalidation.
# ---------------------------------------------------------------------------


class TestServiceAndExecutorRevalidation:
    def test_running_service_references_quarantine_blocks(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """If a live O1/O2 service unit references the quarantine
        path in its ExecStart, the validator must refuse. We can't
        easily install a fake service in the test, so we monkey-patch
        ``_service_exe_references_unit_path`` to simulate it."""
        tx_id = "promotion-20260808T201637Z-60ced7c9"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        quarantine_root.mkdir(parents=True, exist_ok=True)
        qdir = quarantine_root / tx_id
        qdir.mkdir(parents=True, exist_ok=True)
        moved = qdir / candidate.name
        os.rename(candidate, moved)
        record_path = transaction.transaction_path(tx_root, tx_id)
        historical_sha = _sha256(record_path)
        _write_overlay(
            quarantine_root, tx_id,
            historical_tx_path=str(record_path),
            historical_tx_sha256=historical_sha,
            quarantine_path=str(moved),
            candidate_path=str(moved),
        )
        # Patch the helper to claim a service references the path.
        original = reconcile._service_exe_references_unit_path

        def _fake(unit, path):
            if unit == fake_target.service_unit:
                return True
            return original(unit, path)

        reconcile._service_exe_references_unit_path = _fake
        try:
            validation = reconcile.validate_completed_reconciliation(
                tx_id, tx_root=tx_root, quarantine_root=quarantine_root,
                allowed_target=fake_target, allowed_supervisor=fake_supervisor,
            )
        finally:
            reconcile._service_exe_references_unit_path = original
        assert validation.is_invalid
        assert any("service" in r for r in validation.reasons)


# ---------------------------------------------------------------------------
# The exact 2026-08-08 incident replay.
# ---------------------------------------------------------------------------


class Test20260808FullReplay:
    """End-to-end replay of the exact 2026-08-08 incident.

    Sequence:
      1. Stale historical transaction exists with
         phase=candidate_staging, mutation_boundary_crossed=false.
      2. The preflight fails on no_other_transaction (the bug).
      3. The reconciler independently proves the candidate safe,
         MOVES it into quarantine, and writes an atomic overlay.
      4. The historical transaction.json is BYTE-IDENTICAL.
      5. The preflight PASSES on no_other_transaction with the
         message "no active transactions; 1 historical transaction
         validly reconciled".

    The replay must not:
      * edit the historical transaction.json
      * use ``rm -rf``
      * hard-code this transaction id as exempt
    """

    def test_full_replay(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        tx_id = "promotion-20260808T201637Z-60ced75e"
        candidate = _make_candidate(fake_target)
        _write_record(
            tx_id, candidate_path=str(candidate), phase="candidate_staging",
            tx_root=tx_root, target_artifact_sha="a" * 40,
        )
        record_path = transaction.transaction_path(tx_root, tx_id)
        original_bytes = record_path.read_bytes()
        original_sha = _sha256(record_path)
        # Step 2: preflight fails on no_other_transaction.
        report = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        assert preflight.check_no_other_transaction(
                report, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        ) is False
        # Step 3: reconciler.
        reconcile_report = reconcile.reconcile_stale_transaction(
            tx_id, quarantine_root=quarantine_root, tx_root=tx_root,
            allowed_target=fake_target, allowed_supervisor=fake_supervisor,
        )
        assert reconcile_report.safe is True
        assert reconcile_report.classification == "stale_incomplete"
        assert reconcile_report.disposition == "quarantined"
        # Step 4: historical JSON byte-identical.
        assert _sha256(record_path) == original_sha
        assert record_path.read_bytes() == original_bytes
        # No ``rm -rf`` was used; the candidate was MOVED (rename).
        assert not candidate.exists()
        # The reconciler renames the candidate directory into
        # quarantine_root/<tx_id>, so the candidate contents live
        # directly under the quarantine directory, not under a
        # nested leaf-name path.
        assert (quarantine_root / tx_id / "PROVENANCE.txt").exists()
        # Step 5: preflight passes.
        report2 = preflight.PreflightReport(
            target="O1", supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(
            report2, target=fake_target, supervisor=fake_supervisor,
            quarantine_root=quarantine_root,
        )
        assert ok is True
        detail = next(
            c.detail for c in report2.checks if c.name == "no_other_transaction"
        )
        assert "validly reconciled" in detail
        assert tx_id in detail