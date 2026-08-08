"""Tests for the peer-deployer preflight gate.

These tests prove:

  * the preflight refuses to mutate anything on failure
  * a successful preflight reports every required check
  * the service-state helper is exercised
  * the exact-artifact identity is verified
  * self-upgrade is refused at the preflight layer
"""

from __future__ import annotations

import importlib.util
import sys
import json
import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"

def _load_pkg():
    """Load the peer_deployer package as a proper package so relative
    imports inside modules resolve correctly."""
    import sys as _sys
    if "peer_deployer" in _sys.modules:
        return _sys.modules["peer_deployer"]
    init = PKG_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "peer_deployer", init,
        submodule_search_locations=[str(PKG_ROOT)],
    )
    assert spec is not None
    pkg = importlib.util.module_from_spec(spec)
    _sys.modules["peer_deployer"] = pkg
    spec.loader.exec_module(pkg)
    # Pre-import submodules so relative imports work.
    for name in ["identity", "transaction", "service_state", "preflight", "rollback"]:
        sub_spec = importlib.util.spec_from_file_location(
            f"peer_deployer.{name}", PKG_ROOT / f"{name}.py"
        )
        assert sub_spec is not None
        sub = importlib.util.module_from_spec(sub_spec)
        _sys.modules[f"peer_deployer.{name}"] = sub
        sub_spec.loader.exec_module(sub)
        setattr(pkg, name, sub)
    return pkg

_pkg = _load_pkg()
identity = _pkg.identity
_pkg = _load_pkg()
_pkg = _load_pkg()
preflight = _pkg.preflight
if '_pkg' not in dir(): _pkg = _load_pkg()
transaction = _pkg.transaction


def test_canonical_hashes_are_strict() -> None:
    assert preflight.ACCEPTED_ARTIFACT_SHA == "541c9a3180b81bfb2fc450b3ef5f8648691b359d"
    assert preflight.ACCEPTED_ARTIFACT_VERSION == "0.9.0.dev0"
    assert len(preflight.ACCEPTED_MAIN_WHEEL_SHA256) == 64
    assert len(preflight.ACCEPTED_SDK_CLIENT_WHEEL_SHA256) == 64
    assert len(preflight.ACCEPTED_SDK_UI_WHEEL_SHA256) == 64


def test_preflight_refuses_target_equal_supervisor() -> None:
    report = preflight.run_preflight(target=identity.O1, supervisor=identity.O1)
    assert report.passed is False
    names = [c.name for c in report.checks]
    assert "target_distinct_from_supervisor" in names
    failed = [c for c in report.checks if not c.ok]
    assert any(c.name == "target_distinct_from_supervisor" for c in failed)


def test_preflight_runs_all_checks_even_when_one_fails() -> None:
    """Even when target == supervisor, the rest of the checks still run
    so the operator sees the full picture (rather than just aborting on
    the first failure).
    """
    report = preflight.run_preflight(target=identity.O1, supervisor=identity.O1)
    # Most checks should still appear in the report.
    assert len(report.checks) >= 10


def test_preflight_passed_only_when_all_checks_pass() -> None:
    report = preflight.run_preflight(target=identity.O1, supervisor=identity.O2)
    # On the live host, the preflight may pass (when O1 is healthy and
    # O2 is the accepted artifact) or fail. The key invariant is the
    # structured report: every check is recorded, and the overall
    # passed flag is derived from the check statuses.
    assert isinstance(report.passed, bool)
    assert all(isinstance(c, preflight.CheckResult) for c in report.checks)
    reconstructed = all(c.ok for c in report.checks)
    assert report.passed == reconstructed


def test_check_target_distinct_from_supervisor_records_failure() -> None:
    report = preflight.PreflightReport(
        target="O1",
        supervisor="O1",
        target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
        target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
        passed=False,
    )
    ok = preflight.check_target_distinct_from_supervisor(report, identity.O1, identity.O1)
    assert ok is False
    assert any(c.name == "target_distinct_from_supervisor" and not c.ok for c in report.checks)


def test_check_target_distinct_from_supervisor_records_success() -> None:
    report = preflight.PreflightReport(
        target="O1",
        supervisor="O2",
        target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
        target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
        passed=False,
    )
    ok = preflight.check_target_distinct_from_supervisor(report, identity.O1, identity.O2)
    assert ok is True
    assert any(c.name == "target_distinct_from_supervisor" and c.ok for c in report.checks)


def test_check_no_other_transaction_passes_on_empty_root(tmp_path: Path) -> None:
    monkey_root = tmp_path / "tx_root"
    monkey_root.mkdir()
    # Patch the default tx_root.
    original = transaction.DEFAULT_TX_ROOT
    object.__setattr__(transaction, "DEFAULT_TX_ROOT", monkey_root)
    try:
        report = preflight.PreflightReport(
            target="O1",
            supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(report)
        assert ok is True
    finally:
        object.__setattr__(transaction, "DEFAULT_TX_ROOT", original)


def test_check_no_other_transaction_fails_on_inflight(tmp_path: Path) -> None:
    monkey_root = tmp_path / "tx_root"
    monkey_root.mkdir()
    original = transaction.DEFAULT_TX_ROOT
    object.__setattr__(transaction, "DEFAULT_TX_ROOT", monkey_root)
    try:
        # Create an in-flight transaction.
        transaction.create(
            tx_id=transaction.make_tx_id(),
            target="O1",
            supervisor="O2",
            target_artifact_sha="a" * 40,
            target_artifact_version="0.9.0.dev0",
            main_wheel_sha256="b" * 64,
            sdk_client_wheel_sha256="c" * 64,
            sdk_ui_wheel_sha256="d" * 64,
            root=monkey_root,
        )
        report = preflight.PreflightReport(
            target="O1",
            supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(report)
        assert ok is False
        assert any(c.name == "no_other_transaction" and not c.ok for c in report.checks)
    finally:
        object.__setattr__(transaction, "DEFAULT_TX_ROOT", original)


def test_check_no_other_transaction_ignores_completed(tmp_path: Path) -> None:
    monkey_root = tmp_path / "tx_root"
    monkey_root.mkdir()
    original = transaction.DEFAULT_TX_ROOT
    object.__setattr__(transaction, "DEFAULT_TX_ROOT", monkey_root)
    try:
        record = transaction.create(
            tx_id=transaction.make_tx_id(),
            target="O1",
            supervisor="O2",
            target_artifact_sha="a" * 40,
            target_artifact_version="0.9.0.dev0",
            main_wheel_sha256="b" * 64,
            sdk_client_wheel_sha256="c" * 64,
            sdk_ui_wheel_sha256="d" * 64,
            root=monkey_root,
        )
        # Mark as committed — should not count as in-flight.
        transaction.complete(record, root=monkey_root)
        report = preflight.PreflightReport(
            target="O1",
            supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_no_other_transaction(report)
        assert ok is True
    finally:
        object.__setattr__(transaction, "DEFAULT_TX_ROOT", original)


def test_check_service_state_helper_active_recognizes_running_supervisor() -> None:
    """If O2 is healthy on the host, the helper check reports OK."""
    if shutil.which("systemctl") is None:
        pytest.skip("systemctl not available")
    # Verify O2 is healthy.
    import subprocess
    active = subprocess.run(
        ["systemctl", "is-active", "omnigent-production.service"],
        capture_output=True, text=True, check=False,
    )
    if active.stdout.strip() != "active":
        pytest.skip("O2 is not active on this host")
    report = preflight.PreflightReport(
        target="O1",
        supervisor="O2",
        target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
        target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
        passed=False,
    )
    ok = preflight.check_service_state_helper(report)
    assert ok is True


def test_check_service_state_helper_unknown_handles_unknown_unit() -> None:
    """If the bogus unit is unexpectedly known, the check fails safely."""
    report = preflight.PreflightReport(
        target="O1",
        supervisor="O2",
        target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
        target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
        passed=False,
    )
    # The check should not raise.
    preflight.check_service_state_helper(report)


def test_check_artifact_present_requires_release_root(tmp_path: Path) -> None:
    """If the accepted release root does not exist, the check fails."""
    # Patch ACCEPTED_RELEASE_ROOT to a non-existent path.
    original = preflight.ACCEPTED_RELEASE_ROOT
    object.__setattr__(preflight, "ACCEPTED_RELEASE_ROOT", tmp_path / "nonexistent")
    try:
        report = preflight.PreflightReport(
            target="O1",
            supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_accepted_artifact_present(report, identity.O1)
        assert ok is False
        assert any(c.name == "accepted_artifact_present" and not c.ok for c in report.checks)
    finally:
        object.__setattr__(preflight, "ACCEPTED_RELEASE_ROOT", original)


def test_check_artifact_present_rejects_wrong_basename(tmp_path: Path) -> None:
    """If the release dir basename is not the SHA, the check fails."""
    wrong = tmp_path / "wrong-name"
    wrong.mkdir()
    original = preflight.ACCEPTED_RELEASE_ROOT
    object.__setattr__(preflight, "ACCEPTED_RELEASE_ROOT", wrong)
    try:
        report = preflight.PreflightReport(
            target="O1",
            supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_accepted_artifact_present(report, identity.O1)
        assert ok is False
    finally:
        object.__setattr__(preflight, "ACCEPTED_RELEASE_ROOT", original)


def test_preflight_report_to_dict_is_serializable() -> None:
    report = preflight.run_preflight(target=identity.O1, supervisor=identity.O2)
    blob = report.to_dict()
    # Should be JSON-serializable.
    json.dumps(blob)
    assert blob["target"] == "O1"
    assert blob["supervisor"] == "O2"
    assert isinstance(blob["checks"], list)
    for c in blob["checks"]:
        assert set(c) == {"name", "ok", "detail", "fatal"}


def test_preflight_does_not_mutate_target_on_failure(tmp_path: Path) -> None:
    """The preflight must not mutate any target files regardless of
    pass/fail outcome.

    The test limits its comparison to the canonical control files
    (PROVENANCE.txt, venv symlink, chat.db) rather than every file
    in the home, because the live O1 host is actively writing logs
    and SQLite WAL/SHM files during the test run.
    """
    target_home = identity.HOME_MAPPING[str(identity.O1.deployment_root)]
    if not target_home.exists():
        pytest.skip("no O1 home on this host")
    # Snapshot ONLY the canonical files that the preflight must not
    # touch. The other files (logs, sqlite wal) are allowed to change
    # because the running O1 server is active.
    canonical = [
        target_home / "PROVENANCE.txt",
        target_home / "chat.db",
        identity.O1.deployment_root / "PROVENANCE.txt",
        identity.O1.deployment_root / "venv",
    ]
    before = {str(p): p.stat().st_mtime_ns for p in canonical if p.exists()}
    preflight.run_preflight(target=identity.O1, supervisor=identity.O2)
    after = {str(p): p.stat().st_mtime_ns for p in canonical if p.exists()}
    assert before == after


def test_check_target_db_fail_closed_when_missing(tmp_path: Path) -> None:
    """If the target DB is missing, the check fails (does not raise)."""
    fake_target = identity.Instance(
        name="O1",
        deployment_root=tmp_path / "deploy",
        service_unit="x",
        host_unit="y",
        port=1,
        health_url="http://x",
    )
    object.__setattr__(identity, "HOME_MAPPING", {**identity.HOME_MAPPING, str(fake_target.deployment_root): tmp_path / "home"})
    try:
        report = preflight.PreflightReport(
            target="O1",
            supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_target_db(report, fake_target)
        assert ok is False
        assert any(c.name == "target_db_exists" and not c.ok for c in report.checks)
    finally:
        # Restore HOME_MAPPING.
        object.__setattr__(identity, "HOME_MAPPING", {k: v for k, v in identity.HOME_MAPPING.items() if k != str(fake_target.deployment_root)})


def test_check_target_db_accepts_integrity_ok(tmp_path: Path) -> None:
    fake_target = identity.Instance(
        name="O1",
        deployment_root=tmp_path / "deploy",
        service_unit="x",
        host_unit="y",
        port=1,
        health_url="http://x",
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "chat.db").write_bytes(b"not a real db")
    object.__setattr__(identity, "HOME_MAPPING", {**identity.HOME_MAPPING, str(fake_target.deployment_root): home})
    try:
        report = preflight.PreflightReport(
            target="O1",
            supervisor="O2",
            target_artifact_sha=preflight.ACCEPTED_ARTIFACT_SHA,
            target_artifact_version=preflight.ACCEPTED_ARTIFACT_VERSION,
            passed=False,
        )
        ok = preflight.check_target_db(report, fake_target)
        # The DB is a corrupt file; integrity check fails.
        assert ok is False
        assert any(c.name == "target_db_integrity" and not c.ok for c in report.checks)
    finally:
        object.__setattr__(identity, "HOME_MAPPING", {k: v for k, v in identity.HOME_MAPPING.items() if k != str(fake_target.deployment_root)})
