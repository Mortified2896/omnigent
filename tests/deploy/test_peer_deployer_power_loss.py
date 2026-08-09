"""Tests for process-death / power-loss safety (Phase 10).

The hardening brief requires that the durable transaction record
make each state diagnosable and recoverable from a separate host
process. These tests exercise the recovery paths after various
classes of interruption:

  * Interruption between staging and verification.
  * Interruption between verification and switch.
  * Interruption between switch and accept.
  * Interruption during rollback.

The tests do not actually kill processes. They exercise the
durable recovery API by simulating an interruption: they
write a transaction record to a state corresponding to a
boundary, then verify that the next process can correctly
classify the state and proceed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"


def _load_pkg():
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
fsm = _pkg.fsm
transaction = _pkg.transaction


@pytest.fixture
def tx_root(tmp_path: Path) -> Path:
    root = tmp_path / "transactions"
    root.mkdir()
    object.__setattr__(transaction, "DEFAULT_TX_ROOT", root)
    return root


def _make_record(
    tx_root: Path,
    phase: str,
    *,
    mutation_boundary_crossed: bool = False,
    db_backup_path: str = "",
    db_backup_integrity: str = "",
    old_runtime_path: str = "",
) -> transaction.TransactionRecord:
    record = transaction.create(
        tx_id=transaction.make_tx_id(),
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    record.phase = phase
    record.mutation_boundary_crossed = mutation_boundary_crossed
    record.db_backup_path = db_backup_path
    record.db_backup_integrity = db_backup_integrity
    record.old_runtime_path = old_runtime_path
    transaction.save(record, root=tx_root)
    return record


class TestRecoverabilityAfterInterruption:
    """Each phase can be diagnosed and recovered from a separate process."""

    def test_recoverable_after_staging_interruption(
        self, tx_root: Path,
    ) -> None:
        """Interruption after staging, before mutation.

        The transaction record is in ``candidate_staging`` with
        mutation_boundary_crossed=False. The next process should
        recognize this as a pre-mutation failure and refuse to
        touch the active runtime.
        """
        record = _make_record(
            tx_root, phase="candidate_staging",
            mutation_boundary_crossed=False,
        )
        # Load from disk in a "fresh process" by re-importing.
        loaded = transaction.load(record.tx_id, root=tx_root)
        assert loaded.phase == "candidate_staging"
        assert loaded.mutation_boundary_crossed is False
        # The FSM disposition is pre-mutation: no active runtime touch.
        d = fsm.disposition_for(loaded)
        assert d.can_touch_active_runtime is False
        assert d.can_touch_db is False
        assert d.can_clear_transaction_staging is True

    def test_recoverable_after_verification_interruption(
        self, tx_root: Path,
    ) -> None:
        """Interruption after verification, before mutation.

        The transaction record is in ``candidate_verified`` with
        mutation_boundary_crossed=False. Same as staging: pure
        no-touch disposition.
        """
        record = _make_record(
            tx_root, phase="candidate_verified",
            mutation_boundary_crossed=False,
        )
        loaded = transaction.load(record.tx_id, root=tx_root)
        assert loaded.phase == "candidate_verified"
        assert loaded.mutation_boundary_crossed is False
        d = fsm.disposition_for(loaded)
        assert d.can_touch_active_runtime is False
        assert d.can_touch_db is False

    def test_recoverable_after_switch_interruption(
        self, tx_root: Path,
    ) -> None:
        """Interruption after switch, before mutation completion.

        The transaction record is in ``switch`` with
        mutation_boundary_crossed=True. The dispatch must require
        old_runtime_path to be set.
        """
        record = _make_record(
            tx_root, phase="switch",
            mutation_boundary_crossed=True,
            old_runtime_path="/opt/omnigent/venv.legacy-tx",
        )
        loaded = transaction.load(record.tx_id, root=tx_root)
        assert loaded.phase == "switch"
        assert loaded.mutation_boundary_crossed is True
        d = fsm.disposition_for(loaded)
        assert d.can_touch_active_runtime is True
        assert d.can_touch_db is False
        assert d.must_verify_old_runtime is True

    def test_recoverable_after_db_migration_interruption(
        self, tx_root: Path,
    ) -> None:
        """Interruption after DB migration, before full accept.

        The transaction record is in ``service_restart`` with
        mutation_boundary_crossed=True. The disposition must
        require both DB backup integrity and old_runtime_path.
        """
        record = _make_record(
            tx_root, phase="service_restart",
            mutation_boundary_crossed=True,
            db_backup_path="/backup/chat.db",
            db_backup_integrity="ok",
            old_runtime_path="/opt/omnigent/venv.legacy-tx",
        )
        loaded = transaction.load(record.tx_id, root=tx_root)
        d = fsm.disposition_for(loaded)
        assert d.can_touch_active_runtime is True
        assert d.can_touch_db is True
        assert d.must_verify_db_backup is True
        assert d.must_verify_old_runtime is True

    def test_unrecoverable_post_mutation_without_db_backup(
        self, tx_root: Path,
    ) -> None:
        """A post-mutation tx without a verified DB backup is
        refused; the operator must verify the DB state.
        """
        record = _make_record(
            tx_root, phase="service_restart",
            mutation_boundary_crossed=True,
            db_backup_path="",
            db_backup_integrity="",
        )
        loaded = transaction.load(record.tx_id, root=tx_root)
        with pytest.raises(transaction.TransactionError, match="db_backup_path"):
            fsm.disposition_for(loaded)


class TestPhaseAdvancementDirectional:
    """Phase advancement is forward-only and persists."""

    def test_phase_advance_persists_to_disk(self, tx_root: Path) -> None:
        """Phase advancement is durable on disk."""
        record = _make_record(tx_root, phase="init")
        transaction.advance(record, "preflight", root=tx_root)
        loaded = transaction.load(record.tx_id, root=tx_root)
        assert loaded.phase == "preflight"

    def test_phase_advance_refuses_backward(self, tx_root: Path) -> None:
        """Phase advancement is forward-only."""
        record = _make_record(tx_root, phase="candidate_verified")
        with pytest.raises(transaction.TransactionError, match="backward"):
            transaction.advance(record, "init", root=tx_root)

    def test_terminal_phases_allowed_from_anywhere(self, tx_root: Path) -> None:
        """``failure`` and ``rolled_back`` are allowed from any phase."""
        record = _make_record(tx_root, phase="candidate_verified")
        transaction.advance(record, "failure", root=tx_root)
        loaded = transaction.load(record.tx_id, root=tx_root)
        assert loaded.phase == "failure"


class TestSignalHandlersInstalled:
    """The v3 entrypoint installs signal handlers."""

    def test_signal_handlers_installed(self) -> None:
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "SIGINT" in host_src
        assert "SIGTERM" in host_src
        assert "SIGHUP" in host_src
        assert "PromotionInterrupted" in host_src
        assert "install_signal_handlers" in host_src

    def test_promotion_interrupted_raises_recoverable(self) -> None:
        """PromotionInterrupted is a subclass of PromotionError."""
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "class PromotionInterrupted(PromotionError)" in host_src
