"""Tests for the peer-deployer failure-state machine.

The FSM maps the durable transaction phase to a rollback
disposition. Every state must be explicit; every disposition must
state what is allowed and what is forbidden. The tests assert
that the disposition is correctly computed for each phase and
that the integrity checks reject incomplete metadata.
"""
from __future__ import annotations

import importlib.util
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
def empty_tx(tmp_path: Path) -> transaction.TransactionRecord:
    return transaction.create(
        tx_id=transaction.make_tx_id(),
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tmp_path,
    )


class TestPreMutationStates:
    """Pre-mutation states must NEVER touch active runtime or DB."""

    @pytest.mark.parametrize("phase", [
        "init", "preflight", "schema_snapshot", "db_backup",
    ])
    def test_pre_mutation_disposition_no_touch(
        self, empty_tx: transaction.TransactionRecord, phase: str,
    ) -> None:
        empty_tx.phase = phase
        # db_backup just requires the backup path be filled in;
        # schema_snapshot/init/preflight don't require anything.
        if phase == "db_backup":
            empty_tx.db_backup_path = "irrelevant"
        d = fsm.disposition_for(empty_tx)
        assert d.can_touch_active_runtime is False
        assert d.can_touch_db is False
        assert d.can_stop_services is False
        assert d.can_clear_transaction_staging is True

    @pytest.mark.parametrize("phase", [
        "candidate_staging", "candidate_verified",
    ])
    def test_staged_disposition_only_clean_staging(
        self, empty_tx: transaction.TransactionRecord, phase: str,
    ) -> None:
        empty_tx.phase = phase
        d = fsm.disposition_for(empty_tx)
        assert d.can_touch_active_runtime is False
        assert d.can_touch_db is False
        assert d.can_clear_transaction_staging is True


class TestPostMutationStates:
    """Post-mutation states must require DB backup integrity."""

    def test_switched_disposition_requires_old_runtime(
        self, empty_tx: transaction.TransactionRecord,
    ) -> None:
        empty_tx.phase = "switch"
        empty_tx.old_runtime_path = "/opt/omnigent/venv.legacy-tx123"
        d = fsm.disposition_for(empty_tx)
        assert d.can_touch_active_runtime is True
        assert d.can_touch_db is False
        assert d.must_verify_old_runtime is True

    def test_switched_disposition_refuses_no_old_runtime(
        self, empty_tx: transaction.TransactionRecord,
    ) -> None:
        empty_tx.phase = "switch"
        empty_tx.old_runtime_path = ""
        with pytest.raises(transaction.TransactionError, match="old_runtime_path"):
            fsm.disposition_for(empty_tx)

    def test_db_migrated_disposition_requires_db_backup(
        self, empty_tx: transaction.TransactionRecord,
    ) -> None:
        empty_tx.phase = "service_restart"
        empty_tx.old_runtime_path = "/opt/omnigent/venv.legacy-tx"
        empty_tx.db_backup_path = "/backup/chat.db"
        empty_tx.db_backup_integrity = "ok"
        d = fsm.disposition_for(empty_tx)
        assert d.can_touch_active_runtime is True
        assert d.can_touch_db is True
        assert d.must_verify_db_backup is True

    def test_db_migrated_disposition_refuses_no_db_backup(
        self, empty_tx: transaction.TransactionRecord,
    ) -> None:
        empty_tx.phase = "service_restart"
        empty_tx.old_runtime_path = "/opt/omnigent/venv.legacy-tx"
        empty_tx.db_backup_path = ""
        with pytest.raises(transaction.TransactionError, match="db_backup_path"):
            fsm.disposition_for(empty_tx)

    def test_db_migrated_disposition_refuses_bad_integrity(
        self, empty_tx: transaction.TransactionRecord,
    ) -> None:
        empty_tx.phase = "service_restart"
        empty_tx.old_runtime_path = "/opt/omnigent/venv.legacy-tx"
        empty_tx.db_backup_path = "/backup/chat.db"
        empty_tx.db_backup_integrity = "fail"
        with pytest.raises(transaction.TransactionError, match="db_backup_integrity"):
            fsm.disposition_for(empty_tx)


class TestTerminalStates:
    """Terminal states must refuse any destructive operation."""

    @pytest.mark.parametrize("phase", ["tx_committed", "rolled_back", "failure"])
    def test_terminal_disposition_no_touch(
        self, empty_tx: transaction.TransactionRecord, phase: str,
    ) -> None:
        empty_tx.phase = phase
        d = fsm.disposition_for(empty_tx)
        assert d.can_touch_active_runtime is False
        assert d.can_touch_db is False
        assert d.can_stop_services is False
        assert d.can_clear_transaction_staging is False


class TestUnknownPhase:
    """Unknown phase must refuse unconditionally."""

    def test_unknown_phase_refuses(self, empty_tx: transaction.TransactionRecord) -> None:
        empty_tx.phase = "NEVER_DEFINED"
        with pytest.raises(transaction.TransactionError, match="unknown"):
            fsm.disposition_for(empty_tx)


class TestVerifyOldRuntimePath:
    """The verify_old_runtime_path helper guards against bad paths."""

    def test_accepts_tx_specific_legacy(self) -> None:
        p = fsm.verify_old_runtime_path("/opt/omnigent/venv.legacy-tx12345")
        assert p.name == "venv.legacy-tx12345"

    def test_rejects_empty(self) -> None:
        with pytest.raises(transaction.TransactionError):
            fsm.verify_old_runtime_path("")

    def test_rejects_relative(self) -> None:
        with pytest.raises(transaction.TransactionError, match="absolute"):
            fsm.verify_old_runtime_path("venv.legacy-tx")

    def test_rejects_non_legacy_name(self) -> None:
        with pytest.raises(transaction.TransactionError, match="legacy"):
            fsm.verify_old_runtime_path("/opt/omnigent/venv")


class TestClassifyPhase:
    """The classify_phase helper maps phases to states."""

    def test_known_phases_map(self) -> None:
        assert fsm.classify_phase("init") == fsm.State.PREFLIGHT
        assert fsm.classify_phase("candidate_staging") == fsm.State.STAGED
        assert fsm.classify_phase("switch") == fsm.State.SWITCHED
        assert fsm.classify_phase("tx_committed") == fsm.State.ACCEPTED

    def test_unknown_phase_returns_none(self) -> None:
        assert fsm.classify_phase("NEVER_DEFINED") is None
