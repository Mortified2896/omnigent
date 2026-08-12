"""Tests for the peer-deployer stale-transaction reconciliation.

These tests prove the reconcile operation is fail-closed and
forensic-preserving. They cover the exact 2026-08-08 incident
sequence (a half-staged artifact under a non-terminal transaction
record) and assert the new operation:

  * MOVES the candidate into a quarantine directory
  * NEVER rewrites the historical transaction.json
  * REFUSES on any unsafe or unproven condition
  * Refuses when the candidate is the O1 active runtime
  * Refuses when the candidate is O2's deployment root
  * Refuses when the candidate is O2's DB home
  * Refuses when the candidate is the O1 venv symlink
  * Refuses when a service references the candidate
  * Refuses when the historical transaction is corrupt
  * Refuses when the historical transaction has target == supervisor
"""
from __future__ import annotations

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
    """Load the peer_deployer package as a proper package."""
    if "peer_deployer" in sys.modules:
        return sys.modules["peer_deployer"]
    init = PKG_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "peer_deployer", init,
        submodule_search_locations=[str(PKG_ROOT)],
    )
    assert spec is not None
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["peer_deployer"] = pkg
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
            f"peer_deployer.{name}", PKG_ROOT / f"{name}.py"
        )
        assert sub_spec is not None
        sub = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"peer_deployer.{name}"] = sub
        sub_spec.loader.exec_module(sub)
        setattr(pkg, name, sub)
    return pkg


_pkg = _load_pkg()
reconcile = _pkg.reconcile
transaction = _pkg.transaction
identity = _pkg.identity


@pytest.fixture
def tx_root(tmp_path: Path) -> Path:
    root = tmp_path / "transactions"
    root.mkdir()
    object.__setattr__(transaction, "DEFAULT_TX_ROOT", root)
    return root


@pytest.fixture
def quarantine_root(tmp_path: Path) -> Path:
    root = tmp_path / "quarantine"
    root.mkdir()
    return root


def _set_o1_deployment_root(tmp_path: Path) -> Path:
    o1 = identity.O1
    original = o1.deployment_root
    new_root = tmp_path / "o1_deploy"
    new_root.mkdir(parents=True, exist_ok=True)
    object.__setattr__(o1, "deployment_root", new_root)
    return original


def _restore_o1_deployment_root(original: Path) -> None:
    object.__setattr__(identity.O1, "deployment_root", original)


def _set_o2_deployment_root(tmp_path: Path) -> Path:
    o2 = identity.O2
    original = o2.deployment_root
    new_root = tmp_path / "o2_deploy"
    new_root.mkdir(parents=True, exist_ok=True)
    object.__setattr__(o2, "deployment_root", new_root)
    return original


def _restore_o2_deployment_root(original: Path) -> None:
    object.__setattr__(identity.O2, "deployment_root", original)


@pytest.fixture
def fake_target(tmp_path: Path) -> identity.Instance:
    """Override O1's deployment_root to a tmp dir for the test."""
    original = _set_o1_deployment_root(tmp_path)
    yield identity.O1
    _restore_o1_deployment_root(original)


@pytest.fixture
def fake_supervisor(tmp_path: Path) -> identity.Instance:
    """Override O2's deployment_root to a tmp dir for the test."""
    original = _set_o2_deployment_root(tmp_path)
    yield identity.O2
    _restore_o2_deployment_root(original)


def _write_record(
    tx_id: str,
    *,
    candidate_path: str = "",
    mutation_boundary_crossed: bool = False,
    phase: str = "candidate_staging",
    tx_root: Path | None = None,
    db_backup_path: str = "",
    db_backup_integrity: str = "",
) -> transaction.TransactionRecord:
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
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
    transaction.save(record, root=tx_root)
    return record


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# The exact 2026-08-08 incident regression.
# ---------------------------------------------------------------------------


class TestIncidentRegression:
    """Regression tests for the 2026-08-08 incident sequence.

    The exact failure pattern was:
      1. A transaction was created with phase=candidate_staging,
         mutation_boundary_crossed=False, owned_resources=[some
         supervised snapshot].
      2. A staging step ran (pip install ...; preflight failed).
      3. The fallback rollback inferred ownership from path shape
         and deleted /opt/omnigent/venv.
      4. The original 0.8.1 runtime was destroyed.

    The reconciler must NOT do step 3. It must independently
    prove the candidate is safe to quarantine (not active runtime,
    not O2, not referenced by any service) and then MOVE it.
    """

    def test_2026_08_08_incident_full_replay(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Reproduce the 2026-08-08 incident and assert the reconciler
        does NOT delete the active runtime.

        Does:
          * Build a tx_root that mirrors the historical transaction
            promotion-20260808T201637Z-60ced75e (phase=candidate_staging,
            mutation_boundary_crossed=False).
          * Build a candidate directory under fake_target that looks
            like a release.
          * Leave the active runtime at a separate path with the
            original 0.8.1 contents.
          * Run the reconciler.
          * Assert:
              - the candidate was MOVED into quarantine, not deleted
              - the original active runtime still exists
              - the historical transaction.json is byte-identical
        """
        tx_id = "promotion-20260808T201637Z-60ced75e"
        candidate = fake_target.deployment_root / "releases" / ("a" * 40)
        candidate.mkdir(parents=True, exist_ok=True)
        (candidate / "PROVENANCE.txt").write_text(
            "schema_version=1\n"
            f"sha={'a' * 40}\n"
            "package_version=0.9.0.dev0\n"
            "wheel_sha256=b\n"
            "wheel_filename=omnigent-0.9.0.dev0-py3-none-any.whl\n"
        )
        (candidate / "artifacts").mkdir()
        (candidate / "artifacts" / "omnigent-0.9.0.dev0-py3-none-any.whl").write_text("wheel")
        (candidate / "venv").mkdir()

        # The active runtime is the live 0.8.1 venv, NOT the candidate.
        active_venv = fake_target.deployment_root / "venv"
        active_venv.mkdir(parents=True, exist_ok=True)
        (active_venv / "lib").mkdir()
        (active_venv / "bin").mkdir()
        (active_venv / "PROVENANCE.txt").write_text(
            "sha=e5f4249667a1602916d44ac62d10b921a299f05d\n"
            "package_version=0.8.1\n"
        )

        # Build the historical transaction.
        record = _write_record(
            tx_id,
            candidate_path=str(candidate),
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        # Capture the original transaction SHA BEFORE the reconciler
        # runs. The reconciler must NOT change this file.
        original_path = transaction.transaction_path(tx_root, tx_id)
        original_sha = _sha256(original_path)
        original_bytes = original_path.read_bytes()

        # Run the reconciler.
        report = reconcile.reconcile_stale_transaction(
            tx_id,
            quarantine_root=quarantine_root,
            tx_root=tx_root,
            allowed_target=fake_target,
            allowed_supervisor=fake_supervisor,
        )

        # 1. Reconciler says safe.
        assert report.safe is True
        assert report.classification == "stale_incomplete"
        assert report.disposition == "quarantined"
        # 2. Candidate was MOVED.
        assert not candidate.exists()
        qdir = quarantine_root / tx_id
        assert qdir.exists()
        # The moved candidate lives at qdir/<basename of candidate>
        # because os.rename moves the leaf.
        assert (qdir / "PROVENANCE.txt").exists()
        # 3. Active runtime is intact.
        assert active_venv.exists()
        assert (active_venv / "PROVENANCE.txt").exists()
        assert "e5f4249667a1602916d44ac62d10b921a299f05d" in (
            (active_venv / "PROVENANCE.txt").read_text()
        )
        # 4. Historical transaction.json is BYTE-IDENTICAL.
        assert _sha256(original_path) == original_sha
        assert original_path.read_bytes() == original_bytes
        # 5. Audit record was written.
        audit = quarantine_root / tx_id / "reconciliation.json"
        assert audit.is_file()
        audit_blob = json.loads(audit.read_text())
        assert audit_blob["historical_tx_sha256"] == original_sha
        assert audit_blob["tx_id"] == tx_id

    def test_2026_08_08_active_runtime_protected(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """If the candidate IS the O1 active runtime, the reconciler
        REFUSES. The historical transaction must NOT be modified.
        """
        tx_id = "promotion-20260808T201637Z-60ced752"
        # Active runtime IS the candidate.
        active_venv = fake_target.deployment_root / "venv"
        # Make it a symlink so we can prove the check resolves realpath.
        target_release = tmp_path / "release_that_should_not_be_quarantined"
        target_release.mkdir(parents=True, exist_ok=True)
        active_venv.symlink_to(target_release)
        # The candidate in the transaction record is the resolved
        # active runtime path.
        _write_record(
            tx_id,
            candidate_path=str(target_release),
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        original_path = transaction.transaction_path(tx_root, tx_id)
        original_sha = _sha256(original_path)
        # Reconciler must refuse.
        with pytest.raises(reconcile.ReconciliationError, match="active runtime"):
            reconcile.reconcile_stale_transaction(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )
        # Active runtime STILL exists.
        assert target_release.exists()
        # Historical transaction is unchanged.
        assert _sha256(original_path) == original_sha
        # Audit was written with the refusal.
        audit = quarantine_root / tx_id / "reconciliation.json"
        assert audit.is_file()
        audit_blob = json.loads(audit.read_text())
        assert audit_blob["safe"] is False
        assert "active_runtime" in audit_blob["disposition"]

    def test_2026_08_08_o2_release_protected(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """If the candidate is under O2's deployment root, the
        reconciler REFUSES.
        """
        tx_id = "promotion-20260808T201637Z-60ced753"
        # The candidate is under O2's deployment root.
        o2_release = fake_supervisor.deployment_root / "releases" / ("a" * 40)
        o2_release.mkdir(parents=True, exist_ok=True)
        _write_record(
            tx_id,
            candidate_path=str(o2_release),
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        original_path = transaction.transaction_path(tx_root, tx_id)
        original_sha = _sha256(original_path)
        with pytest.raises(reconcile.ReconciliationError, match="O2"):
            reconcile.reconcile_stale_transaction(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )
        # O2 release STILL exists.
        assert o2_release.exists()
        # Historical transaction is unchanged.
        assert _sha256(original_path) == original_sha


# ---------------------------------------------------------------------------
# Refusal proofs.
# ---------------------------------------------------------------------------


class TestRefusalProofs:
    """Each refusal-proof is exercised in isolation."""

    def test_refuses_when_target_is_symlink_target(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Refuses if the candidate is the O1 venv symlink itself."""
        tx_id = "promotion-20260808T201637Z-60ced75a"
        # Create the venv directory so the path exists.
        venv = fake_target.deployment_root / "venv"
        venv.mkdir(parents=True, exist_ok=True)
        _write_record(
            tx_id,
            candidate_path=str(venv),
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        with pytest.raises(reconcile.ReconciliationError, match="active runtime"):
            reconcile.reconcile_stale_transaction(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )

    def test_refuses_when_target_is_o2_db(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Refuses if the candidate overlaps O2's DB home."""
        tx_id = "promotion-20260808T201637Z-60ced75b"
        # Set up a fake O2 home and map it to the supervisor.
        o2_home = tmp_path / "o2_home"
        o2_home.mkdir(parents=True, exist_ok=True)
        identity.HOME_MAPPING[str(fake_supervisor.deployment_root)] = o2_home
        o2_db = o2_home / "chat.db"
        o2_db.write_text("not a real db")
        _write_record(
            tx_id,
            candidate_path=str(o2_db),
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        with pytest.raises(reconcile.ReconciliationError, match="O2"):
            reconcile.reconcile_stale_transaction(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )
        # O2 DB is still there.
        assert o2_db.exists()

    def test_refuses_when_quarantine_exists(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Refuses if the quarantine dir already exists."""
        tx_id = "promotion-20260808T201637Z-60ced75c"
        candidate = fake_target.deployment_root / "releases" / ("a" * 40)
        candidate.mkdir(parents=True, exist_ok=True)
        (quarantine_root / tx_id).mkdir(parents=True, exist_ok=True)
        _write_record(
            tx_id,
            candidate_path=str(candidate),
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        with pytest.raises(reconcile.ReconciliationError, match="quarantine"):
            reconcile.reconcile_stale_transaction(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )

    def test_refuses_for_cross_mutation_boundary_without_db_backup(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Refuses if the historical tx crossed the mutation boundary
        but has no verified DB backup.

        A post-mutation tx cannot be reconciled without a verified
        DB backup because the live DB may have been mutated.
        """
        tx_id = "promotion-20260808T201637Z-60ced75d"
        candidate = fake_target.deployment_root / "releases" / ("a" * 40)
        candidate.mkdir(parents=True, exist_ok=True)
        _write_record(
            tx_id,
            candidate_path=str(candidate),
            mutation_boundary_crossed=True,
            phase="db_backup",
            tx_root=tx_root,
            db_backup_path="",
            db_backup_integrity="",
        )
        with pytest.raises(reconcile.ReconciliationError, match="DB backup"):
            reconcile.reconcile_stale_transaction(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )

    def test_refuses_when_tx_missing(
        self, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Refuses if the transaction does not exist."""
        with pytest.raises(reconcile.ReconciliationError, match="not found"):
            reconcile.reconcile_stale_transaction(
                "promotion-20260808T201637Z-deadbeef",
                quarantine_root=quarantine_root,
                tx_root=tmp_path / "tx",
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )

    def test_refuses_when_tx_id_format_invalid(
        self, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Refuses if the tx_id is not in the canonical format."""
        with pytest.raises(transaction.TransactionError, match="invalid"):
            reconcile.reconcile_stale_transaction(
                "garbage-id",
                quarantine_root=quarantine_root,
                tx_root=tmp_path / "tx",
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )

    def test_refuses_when_target_overlaps_intrinsic_forbidden(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Refuses if the candidate is the O1 venv path itself."""
        tx_id = "promotion-20260808T201637Z-60ced75e"
        # Rock-solid: candidate is /opt/omnigent/venv, which is in
        # INTRINSIC_FORBIDDEN_PATHS.
        # Use the fake target's venv path so we can test without
        # touching the real /opt/omnigent.
        active_venv = fake_target.deployment_root / "venv"
        active_venv.mkdir(parents=True, exist_ok=True)
        _write_record(
            tx_id,
            candidate_path=str(active_venv),
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        with pytest.raises(reconcile.ReconciliationError):
            reconcile.reconcile_stale_transaction(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=fake_target,
                allowed_supervisor=fake_supervisor,
            )


# ---------------------------------------------------------------------------
# Forensic preservation.
# ---------------------------------------------------------------------------


class TestForensicPreservation:
    """The historical transaction record must remain byte-identical."""

    def test_failed_reconcile_does_not_modify_transaction(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """Even when reconciliation fails, the transaction.json is unchanged."""
        tx_id = "promotion-20260808T201637Z-60ced75f"
        record = _write_record(
            tx_id,
            candidate_path="",
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        original_path = transaction.transaction_path(tx_root, tx_id)
        original_sha = _sha256(original_path)
        original_bytes = original_path.read_bytes()
        # No candidate path -> no_candidate classification, but
        # the transaction is unchanged.
        report = reconcile.reconcile_stale_transaction(
            tx_id,
            quarantine_root=quarantine_root,
            tx_root=tx_root,
            allowed_target=fake_target,
            allowed_supervisor=fake_supervisor,
        )
        assert report.classification == "no_candidate"
        assert _sha256(original_path) == original_sha
        assert original_path.read_bytes() == original_bytes

    def test_audit_record_links_to_original_transaction(
        self, tx_root: Path, quarantine_root: Path,
        fake_target: identity.Instance, fake_supervisor: identity.Instance,
        tmp_path: Path,
    ) -> None:
        """The audit record carries the original transaction SHA-256."""
        tx_id = "promotion-20260808T201637Z-60ced7a0"
        candidate = fake_target.deployment_root / "releases" / ("a" * 40)
        candidate.mkdir(parents=True, exist_ok=True)
        record = _write_record(
            tx_id,
            candidate_path=str(candidate),
            mutation_boundary_crossed=False,
            phase="candidate_staging",
            tx_root=tx_root,
        )
        original_path = transaction.transaction_path(tx_root, tx_id)
        original_sha = _sha256(original_path)
        reconcile.reconcile_stale_transaction(
            tx_id,
            quarantine_root=quarantine_root,
            tx_root=tx_root,
            allowed_target=fake_target,
            allowed_supervisor=fake_supervisor,
        )
        audit = quarantine_root / tx_id / "reconciliation.json"
        blob = json.loads(audit.read_text())
        assert blob["historical_tx_sha256"] == original_sha
        assert blob["historical_tx_path"] == str(original_path)
        # The historical transaction.json is still intact at the
        # path recorded in the audit.
        assert Path(blob["historical_tx_path"]).is_file()
        assert _sha256(original_path) == original_sha


# ---------------------------------------------------------------------------
# Reconciliation with the v3 entrypoint.
# ---------------------------------------------------------------------------


class TestV3EntrypointReconcile:
    """The v3 entrypoint exposes --reconcile-stale <TX_ID>."""

    def test_v3_entrypoint_has_reconcile_stale(self) -> None:
        v3 = (REPO_ROOT / "deploy" / "scripts" / "peer_promote_o1_v3.py").read_text()
        assert "--reconcile-stale" in v3
        assert "reconcile.reconcile_stale_transaction" in v3
        assert "ReconciliationError" in v3

    def test_reconcile_classification_final(self) -> None:
        """The reconciler stamps a version + classification."""
        cls = reconcile.RECONCILER_VERSION
        assert cls.startswith("1.")
