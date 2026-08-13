"""Synthetic tests for mode-aware, zero-mutation preflight checks."""

from __future__ import annotations

from pathlib import Path

from deploy.scripts.peer_deployer import identity, preflight, transaction
from deploy.scripts.peer_deployer.mode import DeploymentMode


def _report() -> preflight.PreflightReport:
    return preflight.PreflightReport(
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="1.2.3",
        mode=DeploymentMode.PEER_COPY.value,
    )


def test_preflight_refuses_target_equal_supervisor() -> None:
    report = _report()
    assert not preflight.check_target_distinct_from_supervisor(report, identity.O1, identity.O1)
    assert report.checks[-1].name == "common.target_distinct_from_supervisor"


def test_preflight_accepts_both_safe_directions() -> None:
    for target, supervisor in ((identity.O1, identity.O2), (identity.O2, identity.O1)):
        report = _report()
        assert preflight.check_target_distinct_from_supervisor(report, target, supervisor)


def test_storage_guard_is_injectable_and_requires_active_unlatched() -> None:
    report = _report()
    assert preflight.check_storage_guard(
        report,
        probe=lambda: preflight.StorageGuardStatus(active=True, latched=False, detail="synthetic"),
    )
    assert not preflight.check_storage_guard(
        report,
        probe=lambda: preflight.StorageGuardStatus(
            active=False, latched=False, detail="synthetic"
        ),
    )
    assert not preflight.check_storage_guard(
        report,
        probe=lambda: preflight.StorageGuardStatus(active=True, latched=True, detail="synthetic"),
    )


def test_no_other_transaction_empty_root(tmp_path: Path) -> None:
    original = transaction.DEFAULT_TX_ROOT
    transaction.DEFAULT_TX_ROOT = tmp_path
    try:
        assert preflight.check_no_other_transaction(_report())
    finally:
        transaction.DEFAULT_TX_ROOT = original


def test_no_other_transaction_blocks_active_record(tmp_path: Path) -> None:
    original = transaction.DEFAULT_TX_ROOT
    transaction.DEFAULT_TX_ROOT = tmp_path
    try:
        transaction.create(
            tx_id=transaction.make_tx_id(),
            target="O1",
            supervisor="O2",
            target_artifact_sha="a" * 40,
            target_artifact_version="1.2.3",
            main_wheel_sha256="1" * 64,
            sdk_client_wheel_sha256="2" * 64,
            sdk_ui_wheel_sha256="3" * 64,
            root=tmp_path,
        )
        assert not preflight.check_no_other_transaction(_report())
    finally:
        transaction.DEFAULT_TX_ROOT = original


def test_report_serializes_mode_and_acceptance_reference() -> None:
    report = _report()
    report.acceptance_record_path = "/accepted-artifacts/a/acceptance.json"
    report.acceptance_record_sha256 = "f" * 64
    blob = report.to_dict()
    assert blob["mode"] == DeploymentMode.PEER_COPY.value
    assert blob["acceptance_record_sha256"] == "f" * 64
