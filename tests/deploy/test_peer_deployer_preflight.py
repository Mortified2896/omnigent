"""Synthetic tests for mode-aware, zero-mutation preflight checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_disk_preflight_requires_25_gib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = identity.Instance(
        name="O1",
        deployment_root=tmp_path / "target",
        service_unit="target.service",
        host_unit="target-host.service",
        port=12001,
        health_url="http://127.0.0.1:12001/health",
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(identity.HOME_MAPPING, str(target.deployment_root), home)
    assert preflight.MIN_FREE_BYTES == 25 * 1024**3

    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=preflight.MIN_FREE_BYTES - 1),
    )
    assert not preflight.check_disk_space(_report(), target)
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=preflight.MIN_FREE_BYTES),
    )
    assert preflight.check_disk_space(_report(), target)


def test_python_host_wrapper_only_needs_to_be_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modes: list[int] = []

    def readable(_path: Path, mode: int) -> bool:
        modes.append(mode)
        return mode == preflight.os.R_OK

    monkeypatch.setattr(preflight.os, "access", readable)
    assert preflight.check_scripts_present(_report(), identity.O1)
    assert modes == [preflight.os.R_OK]


def test_report_serializes_mode_and_acceptance_reference() -> None:
    report = _report()
    report.acceptance_record_path = "/accepted-artifacts/a/acceptance.json"
    report.acceptance_record_sha256 = "f" * 64
    blob = report.to_dict()
    assert blob["mode"] == DeploymentMode.PEER_COPY.value
    assert blob["acceptance_record_sha256"] == "f" * 64
