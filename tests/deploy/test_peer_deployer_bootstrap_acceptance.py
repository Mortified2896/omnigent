"""Synthetic coverage for immutable acceptance and first-peer bootstrap."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from deploy.scripts.peer_deployer import acceptance, baseline, identity, preflight, transaction
from deploy.scripts.peer_deployer.mode import DeploymentMode


def _accepted(tmp_path: Path) -> acceptance.CandidateAcceptance:
    sha = "a" * 40
    release = tmp_path / "target" / "releases" / sha
    return acceptance.CandidateAcceptance.create(
        source_sha=sha,
        package_version="1.2.3",
        wheels=(
            acceptance.AcceptedWheel("main", "omnigent.whl", "1" * 64),
            acceptance.AcceptedWheel("sdk_client", "client.whl", "2" * 64),
            acceptance.AcceptedWheel("sdk_ui", "ui.whl", "3" * 64),
        ),
        frontend_root="frontend",
        frontend_tree_sha256="4" * 64,
        immutable_release_root=str(release),
        runtime_venv_path=str(release / "venv"),
        installed_packages=(
            acceptance.InstalledPackage(
                "omnigent", "1.2.3", str(release / "venv/site/omnigent/__init__.py")
            ),
            acceptance.InstalledPackage(
                "omnigent_client", "1.2.3", str(release / "venv/site/omnigent_client/__init__.py")
            ),
            acceptance.InstalledPackage(
                "omnigent_ui_sdk", "1.2.3", str(release / "venv/site/omnigent_ui_sdk/__init__.py")
            ),
        ),
        uv_pip_check_success=True,
        embedded_build_sha=sha,
        boot_command_classification=acceptance.TEMPORARY_PORT_BOOT_CLASSIFICATION,
        temporary_port=54321,
        health_ok=True,
        health_status="ok",
        info_ok=True,
        info_server_version="1.2.3",
        info_build_sha=sha,
        html_assets_ok=True,
        html_asset_count=3,
        disk_headroom_bytes=3 * 1024**3,
        accepted_at="2026-08-09T10:11:12Z",
        builder_identity="builder@host",
        operator_identity="operator@host",
        target_db_schema="schema-new",
    )


def test_acceptance_is_canonical_hashed_and_immutable(tmp_path: Path) -> None:
    record = _accepted(tmp_path)
    path = acceptance.write_immutable(record, root=tmp_path / "accepted-artifacts")
    assert path == tmp_path / "accepted-artifacts" / record.source_sha / "acceptance.json"
    assert acceptance.load(path) == record
    assert acceptance.payload_sha256(record) == record.acceptance_record_sha256
    assert acceptance.write_immutable(record, root=tmp_path / "accepted-artifacts") == path

    blob = json.loads(path.read_text())
    blob["package_version"] = "changed"
    path.chmod(0o644)
    path.write_text(json.dumps(blob))
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.load(path)


def test_missing_and_malformed_acceptance_records_are_refused(tmp_path: Path) -> None:
    with pytest.raises(acceptance.AcceptanceError, match="cannot read"):
        acceptance.load(tmp_path / "missing.json")
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json")
    with pytest.raises(acceptance.AcceptanceError, match="invalid acceptance JSON"):
        acceptance.load(malformed)


def test_live_record_must_be_root_owned_and_read_only(tmp_path: Path) -> None:
    path = acceptance.write_immutable(_accepted(tmp_path), root=tmp_path / "accepted")
    assert path.stat().st_mode & 0o222 == 0
    if path.stat().st_uid != 0:
        with pytest.raises(acceptance.AcceptanceError, match="not root-owned"):
            acceptance.verify_record_permissions(path)


def test_embedded_record_hash_rejects_tampering(tmp_path: Path) -> None:
    record = _accepted(tmp_path)
    with pytest.raises(acceptance.AcceptanceError, match="does not match"):
        replace(record, frontend_tree_sha256="f" * 64).validate()


def test_acceptance_hash_rejects_different_bundle_at_same_version(tmp_path: Path) -> None:
    record = _accepted(tmp_path)
    different = replace(
        record,
        wheels=tuple(
            replace(item, sha256="f" * 64) if item.role == "main" else item
            for item in record.wheels
        ),
    )
    with pytest.raises(acceptance.AcceptanceError, match="does not match"):
        different.validate()


def test_release_verification_refuses_wheel_hash_mismatch(tmp_path: Path) -> None:
    record = _accepted(tmp_path)
    release = Path(record.immutable_release_root)
    artifacts = release / "artifacts"
    artifacts.mkdir(parents=True)
    for wheel in record.wheels:
        (artifacts / wheel.filename).write_bytes(b"wrong")
    failures = acceptance.verify_release(record, release, run_uv_check=False)
    assert any("wheel hash mismatch" in failure for failure in failures)


def test_acceptance_rejects_installed_runtime_identity_mismatch(tmp_path: Path) -> None:
    record = _accepted(tmp_path)
    with pytest.raises(acceptance.AcceptanceError, match="installed omnigent version"):
        replace(
            record,
            installed_packages=tuple(
                replace(item, version="9.9.9") if item.name == "omnigent" else item
                for item in record.installed_packages
            ),
        ).validate(validate_digest=False)


def test_acceptance_rejects_failed_boot_evidence(tmp_path: Path) -> None:
    record = _accepted(tmp_path)
    with pytest.raises(acceptance.AcceptanceError, match="health evidence"):
        replace(record, health_ok=False).validate(validate_digest=False)


def test_storage_guard_must_be_active_and_unlatched() -> None:
    report = preflight.PreflightReport(target="O1", supervisor="O2")
    assert preflight.check_storage_guard(
        report,
        probe=lambda: preflight.StorageGuardStatus(active=True, latched=False),
    )
    assert not preflight.check_storage_guard(
        report,
        probe=lambda: preflight.StorageGuardStatus(active=True, latched=True),
    )


def test_mode_selects_bootstrap_record_or_peer_release(tmp_path: Path) -> None:
    record = _accepted(tmp_path)
    supervisor = identity.Instance(
        name="S",
        deployment_root=tmp_path / "supervisor",
        service_unit="s.service",
        host_unit="s-host.service",
        port=12001,
        health_url="http://127.0.0.1:12001/health",
    )
    assert preflight.source_release_for(
        DeploymentMode.BOOTSTRAP_FIRST_PEER, record, supervisor
    ) == Path(record.immutable_release_root)
    assert (
        preflight.source_release_for(DeploymentMode.PEER_COPY, record, supervisor)
        == supervisor.deployment_root / "releases" / record.source_sha
    )


def test_unhealthy_supervisor_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    report = preflight.PreflightReport(target="O1", supervisor="O2")
    monkeypatch.setattr(preflight.service_state, "is_active", lambda _unit: False)
    monkeypatch.setattr(preflight.identity, "http_health_ok", lambda _url: True)
    assert not preflight.check_supervisor_healthy(report, identity.O2)


def test_peer_copy_requires_supervisor_exact_accepted_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _accepted(tmp_path)
    report = preflight.PreflightReport(target="O2", supervisor="O1")
    monkeypatch.setattr(preflight.identity, "installed_sha", lambda _root: "b" * 40)
    monkeypatch.setattr(
        preflight.identity, "installed_version", lambda _root: record.package_version
    )
    assert not preflight.check_supervisor_identity_matches(report, identity.O1, record)


def test_supervisor_baseline_detects_pid_and_timestamp_drift() -> None:
    unit = baseline.UnitBaseline("server", "active", 10, 100)
    before = baseline.SupervisorBaseline("O2", "a" * 40, "1.2.3", unit, unit)
    after = replace(
        before,
        host=baseline.UnitBaseline("server", "active", 11, 101),
    )
    drift = baseline.compare(before, after)
    assert any("main_pid" in item for item in drift)
    assert any("active_enter_timestamp_monotonic" in item for item in drift)


def test_peer_copy_staging_uses_accepted_wheel_map_and_frontend() -> None:
    cli_source = Path("deploy/scripts/peer_deployer/__main__.py").read_text()
    host_source = Path("deploy/scripts/peer_deployer/host_promotion.py").read_text()
    assert "accepted.wheel_map(supervisor_release)" in cli_source
    assert "accepted_frontend_root=Path(accepted.frontend_root)" in cli_source
    assert "accepted_frontend_root=Path(accepted.frontend_root)" in host_source


def test_current_rollout_modes_are_o1_first_then_o2_peer_copy(tmp_path: Path) -> None:
    record = _accepted(tmp_path)
    assert preflight.source_release_for(
        DeploymentMode.BOOTSTRAP_FIRST_PEER, record, identity.O2
    ) == Path(record.immutable_release_root)
    assert (
        preflight.source_release_for(DeploymentMode.PEER_COPY, record, identity.O1)
        == identity.O1.deployment_root / "releases" / record.source_sha
    )


def test_transaction_distinguishes_references_from_ownership(tmp_path: Path) -> None:
    record = transaction.create(
        tx_id=transaction.make_tx_id(),
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="1.2.3",
        main_wheel_sha256="1" * 64,
        sdk_client_wheel_sha256="2" * 64,
        sdk_ui_wheel_sha256="3" * 64,
        mode=DeploymentMode.BOOTSTRAP_FIRST_PEER.value,
        acceptance_record_path="/accepted-artifacts/acceptance.json",
        acceptance_record_sha256="4" * 64,
        frontend_tree_sha256="5" * 64,
        root=tmp_path,
    )
    transaction.register_referenced(record, "/pre-existing/candidate", root=tmp_path)
    assert record.mode == DeploymentMode.BOOTSTRAP_FIRST_PEER.value
    assert record.acceptance_record_sha256 == "4" * 64
    assert record.frontend_tree_sha256 == "5" * 64
    assert transaction.is_referenced(record, "/pre-existing/candidate")
    assert not transaction.is_owned(record, "/pre-existing/candidate")
    with pytest.raises(transaction.TransactionError, match="referenced"):
        transaction.register_owned(record, "/pre-existing/candidate", root=tmp_path)


def test_legacy_transaction_load_gets_safe_defaults(tmp_path: Path) -> None:
    tx_id = "promotion-20260809T101112Z-0123abcd"
    path = tmp_path / tx_id / "transaction.json"
    path.parent.mkdir()
    legacy = {
        "tx_id": tx_id,
        "target": "O1",
        "supervisor": "O2",
        "target_artifact_sha": "a" * 40,
        "target_artifact_version": "1.2.3",
        "main_wheel_sha256": "1" * 64,
        "sdk_client_wheel_sha256": "2" * 64,
        "sdk_ui_wheel_sha256": "3" * 64,
        "created_at_unix": 1.0,
    }
    path.write_text(json.dumps(legacy))
    loaded = transaction.load(tx_id, root=tmp_path)
    assert loaded.mode == DeploymentMode.PEER_COPY.value
    assert loaded.referenced_resources == []
    assert {item["role"] for item in loaded.exact_wheels} == {
        "main",
        "sdk_client",
        "sdk_ui",
    }
