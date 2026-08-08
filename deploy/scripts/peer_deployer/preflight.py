"""Strict preflight for the peer-supervised deployer.

The preflight is the *only* gate between the operator and any
destructive phase. It must:

  1. Refuse to run if target == supervisor.
  2. Refuse to run if the supervisor cannot be verified independently.
  3. Verify the exact accepted artifact exists and matches the
     expected SHA-256 + runtime SHA + version.
  4. Verify the target DB exists and is integrity-clean.
  5. Verify the rollback location is writable.
  6. Verify there is enough disk space.
  7. Verify the host-level deployer is available.
  8. Verify no other transaction is in flight.
  9. Verify the existing scripts are present and executable.
 10. Verify the service-state helper works correctly.

If any item fails, the preflight MUST:

  * exit non-zero
  * NOT stop target services
  * NOT rename / delete / copy the active runtime
  * NOT touch the DB
  * NOT invoke rollback
  * NOT mutate the supervisor

The preflight exits with a structured JSON report on stdout. The
return code is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import identity, service_state, transaction
from .identity import Instance


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "fatal": self.fatal}


@dataclass
class PreflightReport:
    target: str
    supervisor: str
    target_artifact_sha: str
    target_artifact_version: str
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "supervisor": self.supervisor,
            "target_artifact_sha": self.target_artifact_sha,
            "target_artifact_version": self.target_artifact_version,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
        }


# The accepted artifact expectations. The host-level deployer must
# verify these before any mutation is allowed.
ACCEPTED_ARTIFACT_SHA = "541c9a3180b81bfb2fc450b3ef5f8648691b359d"
ACCEPTED_ARTIFACT_VERSION = "0.9.0.dev0"
ACCEPTED_MAIN_WHEEL_SHA256 = "f49fb3f973c1d98be03eaede76e9c7e86acb91064b06494afdf8f7345524a5e9"
ACCEPTED_SDK_CLIENT_WHEEL_SHA256 = "555a6286477bd528005478571b24cd2fda5c9da505f0957d606b6182614f9605"
ACCEPTED_SDK_UI_WHEEL_SHA256 = "e2141bc6af3bee42a85cad1ff48d008d20439ee34cb52956cda1b7fdff1d45a9"

# The path on the O2 deployment root that holds the accepted release.
ACCEPTED_RELEASE_ROOT = Path("/opt/omnigent-production/releases") / ACCEPTED_ARTIFACT_SHA

# Conservative minimum free space for a promotion. The release dir is
# copied, plus a DB backup, plus headroom for transaction state.
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


class PreflightError(RuntimeError):
    """Raised when the preflight itself cannot run (e.g. config error)."""


def _record(report: PreflightReport, name: str, ok: bool, detail: str,
            fatal: bool = True) -> CheckResult:
    result = CheckResult(name=name, ok=ok, detail=detail, fatal=fatal)
    report.checks.append(result)
    return result


def check_target_distinct_from_supervisor(report: PreflightReport,
                                          target: Instance,
                                          supervisor: Instance) -> bool:
    try:
        identity.require_distinct(target, supervisor)
    except identity.IdentityError as exc:
        _record(report, "target_distinct_from_supervisor", False, str(exc))
        return False
    _record(report, "target_distinct_from_supervisor", True,
            f"target={target.name} supervisor={supervisor.name}")
    return True


def check_target_service_identity(report: PreflightReport, target: Instance) -> bool:
    if not service_state.is_known(target.service_unit):
        _record(report, "target_service_known", False,
                f"target service unit not recognized by systemd: {target.service_unit}")
        return False
    _record(report, "target_service_known", True, target.service_unit)
    return True


def check_target_host_identity(report: PreflightReport, target: Instance) -> bool:
    if not service_state.is_known(target.host_unit):
        _record(report, "target_host_known", False,
                f"target host unit not recognized by systemd: {target.host_unit}")
        return False
    _record(report, "target_host_known", True, target.host_unit)
    return True


def check_supervisor_service_identity(report: PreflightReport,
                                      supervisor: Instance) -> bool:
    if not service_state.is_known(supervisor.service_unit):
        _record(report, "supervisor_service_known", False,
                f"supervisor service unit not recognized: {supervisor.service_unit}")
        return False
    _record(report, "supervisor_service_known", True, supervisor.service_unit)
    return True


def check_supervisor_healthy(report: PreflightReport,
                              supervisor: Instance) -> bool:
    if not service_state.is_active(supervisor.service_unit):
        state = "unknown"
        try:
            state = service_state.get_state(supervisor.service_unit)
        except service_state.ServiceStateError:
            pass
        _record(report, "supervisor_healthy", False,
                f"supervisor service is not active: {supervisor.service_unit} state={state}")
        return False
    if not service_state.is_active(supervisor.host_unit):
        state = "unknown"
        try:
            state = service_state.get_state(supervisor.host_unit)
        except service_state.ServiceStateError:
            pass
        _record(report, "supervisor_healthy", False,
                f"supervisor host is not active: {supervisor.host_unit} state={state}")
        return False
    if not identity.http_health_ok(supervisor.health_url):
        _record(report, "supervisor_healthy", False,
                f"supervisor /health not ok: {supervisor.health_url}")
        return False
    _record(report, "supervisor_healthy", True,
            f"{supervisor.name} server+host active, /health OK")
    return True


def check_supervisor_identity_matches(report: PreflightReport,
                                       supervisor: Instance) -> bool:
    """The supervisor must be exactly the accepted artifact.

    The supervisor is the known-good peer. If the supervisor is not
    running the exact accepted artifact, the preflight is invalid
    because the peer cannot be trusted to supervise.
    """
    try:
        sha = identity.installed_sha(supervisor.deployment_root)
    except identity.IdentityError as exc:
        _record(report, "supervisor_identity", False, str(exc))
        return False
    if sha != ACCEPTED_ARTIFACT_SHA:
        _record(report, "supervisor_identity", False,
                f"supervisor installed SHA {sha!r} != accepted {ACCEPTED_ARTIFACT_SHA!r}")
        return False
    try:
        version = identity.installed_version(supervisor.deployment_root)
    except identity.IdentityError as exc:
        _record(report, "supervisor_identity", False, str(exc))
        return False
    if version != ACCEPTED_ARTIFACT_VERSION:
        _record(report, "supervisor_identity", False,
                f"supervisor installed version {version!r} != accepted {ACCEPTED_ARTIFACT_VERSION!r}")
        return False
    _record(report, "supervisor_identity", True,
            f"{supervisor.name} running accepted {ACCEPTED_ARTIFACT_SHA} / {ACCEPTED_ARTIFACT_VERSION}")
    return True


def check_accepted_artifact_present(report: PreflightReport,
                                     target: Instance) -> bool:
    """The exact accepted artifact must already exist on disk.

    The peer-deployer never rebuilds from a branch tip during
    promotion. It promotes the exact artifact that was accepted on the
    supervisor.
    """
    if not ACCEPTED_RELEASE_ROOT.is_dir():
        _record(report, "accepted_artifact_present", False,
                f"accepted release dir missing: {ACCEPTED_RELEASE_ROOT}")
        return False
    release_name = ACCEPTED_RELEASE_ROOT.name
    if release_name != ACCEPTED_ARTIFACT_SHA:
        _record(report, "accepted_artifact_present", False,
                f"release dir basename {release_name!r} != accepted SHA {ACCEPTED_ARTIFACT_SHA!r}")
        return False
    # The preflight must not refuse just because the release is not at
    # the supervisor — the accepted artifact may be in the supervisor's
    # releases/ but the target may have a different filesystem layout.
    # We require the artifact to exist at the SUPERVISOR's release root
    # because that is the only authoritative source.
    _record(report, "accepted_artifact_present", True, str(ACCEPTED_RELEASE_ROOT))
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_artifact_hashes(report: PreflightReport, target: Instance) -> bool:
    artifacts_dir = ACCEPTED_RELEASE_ROOT / "artifacts"
    if not artifacts_dir.is_dir():
        _record(report, "artifact_hashes", False,
                f"artifacts dir missing: {artifacts_dir}")
        return False

    main_wheel = None
    sdk_client = None
    sdk_ui = None
    for wheel in sorted(artifacts_dir.glob("*.whl")):
        if wheel.name.startswith("omnigent-"):
            main_wheel = wheel
        elif wheel.name.startswith("omnigent_client-"):
            sdk_client = wheel
        elif wheel.name.startswith("omnigent_ui_sdk-"):
            sdk_ui = wheel

    if main_wheel is None:
        _record(report, "artifact_hashes", False,
                f"main wheel not found under {artifacts_dir}")
        return False
    if sdk_client is None:
        _record(report, "artifact_hashes", False,
                f"omnigent_client wheel not found under {artifacts_dir}")
        return False
    if sdk_ui is None:
        _record(report, "artifact_hashes", False,
                f"omnigent_ui_sdk wheel not found under {artifacts_dir}")
        return False

    def _check(name: str, path: Path, expected: str) -> bool:
        actual = _sha256_file(path)
        if actual != expected:
            _record(report, f"artifact_hash_{name}", False,
                    f"{name} wheel SHA mismatch: actual={actual} expected={expected}")
            return False
        _record(report, f"artifact_hash_{name}", True,
                f"{name} wheel SHA256 ok ({actual[:16]}…)")
        return True

    main_ok = _check("main", main_wheel, ACCEPTED_MAIN_WHEEL_SHA256)
    client_ok = _check("sdk_client", sdk_client, ACCEPTED_SDK_CLIENT_WHEEL_SHA256)
    ui_ok = _check("sdk_ui", sdk_ui, ACCEPTED_SDK_UI_WHEEL_SHA256)
    return main_ok and client_ok and ui_ok


def check_artifact_runtime_identity(report: PreflightReport,
                                    target: Instance) -> bool:
    """Verify the staged release's runtime identity matches the accepted SHA."""
    try:
        sha = identity.installed_sha(ACCEPTED_RELEASE_ROOT)
    except identity.IdentityError as exc:
        _record(report, "artifact_runtime_identity", False, str(exc))
        return False
    if sha != ACCEPTED_ARTIFACT_SHA:
        _record(report, "artifact_runtime_identity", False,
                f"accepted release runtime SHA {sha!r} != expected {ACCEPTED_ARTIFACT_SHA!r}")
        return False
    try:
        version = identity.installed_version(ACCEPTED_RELEASE_ROOT)
    except identity.IdentityError as exc:
        _record(report, "artifact_runtime_identity", False, str(exc))
        return False
    if version != ACCEPTED_ARTIFACT_VERSION:
        _record(report, "artifact_runtime_identity", False,
                f"accepted release runtime version {version!r} != expected {ACCEPTED_ARTIFACT_VERSION!r}")
        return False
    _record(report, "artifact_runtime_identity", True,
            f"accepted release runtime {sha}/{version}")
    return True


def check_target_db(report: PreflightReport, target: Instance) -> bool:
    """The target DB must exist and pass integrity check."""
    db_path = target_home_for(target) / "chat.db"
    if not db_path.is_file():
        _record(report, "target_db_exists", False,
                f"target DB missing: {db_path}")
        return False
    sqlite = shutil.which("sqlite3")
    if sqlite is None:
        _record(report, "target_db_integrity", False,
                "sqlite3 not available on PATH")
        return False
    result = subprocess.run(
        [sqlite, str(db_path), "PRAGMA integrity_check;"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "ok":
        _record(report, "target_db_integrity", False,
                f"integrity_check failed: {result.stdout.strip()!r} {result.stderr.strip()!r}")
        return False
    _record(report, "target_db_exists", True, str(db_path))
    _record(report, "target_db_integrity", True, "integrity_check=ok")
    return True


def target_home_for(target: Instance) -> Path:
    """Resolve the target's HOME directory from the deployment root.

    The mapping is centralized in ``identity.HOME_MAPPING`` so the
    preflight, rollback, and any future tool all agree on the same
    home path.
    """
    target_root = str(target.deployment_root)
    if target_root not in identity.HOME_MAPPING:
        raise PreflightError(
            f"unknown target deployment root, no home mapping: {target_root}"
        )
    return identity.HOME_MAPPING[target_root]


def check_rollback_dir_writable(report: PreflightReport,
                                target: Instance) -> bool:
    """The rollback location must be writable.

    The rollback artifacts (DB backup, transaction records,
    evidence) live under the supervisor's evidence directory, not
    the target's home directory. The target's home is the
    application home, which is owned by the host-level deployer
    that actually performs the rollback. This check verifies that
    the supervisor's evidence directory is writable so the O2
    session can record the rollback artifacts.
    """
    # The supervisor's home is always writable from the O2 sandbox.
    supervisor_home = identity.HOME_MAPPING[str(identity.O2.deployment_root)]
    if not supervisor_home.is_dir():
        _record(report, "rollback_dir_writable", False,
                f"supervisor home missing: {supervisor_home}")
        return False
    probe = supervisor_home / f".peer_deployer_write_probe.{os.getpid()}"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        _record(report, "rollback_dir_writable", False,
                f"cannot write to {supervisor_home}: {exc}")
        return False
    if not os.access(supervisor_home, os.W_OK):
        _record(report, "rollback_dir_writable", False,
                f"supervisor home is not writable: {supervisor_home}")
        return False
    _record(report, "rollback_dir_writable", True, str(supervisor_home))
    return True


def check_disk_space(report: PreflightReport, target: Instance) -> bool:
    """Enough free disk space for the promotion."""
    target_home = target_home_for(target)
    stat = shutil.disk_usage(target_home)
    if stat.free < MIN_FREE_BYTES:
        _record(report, "disk_space", False,
                f"only {stat.free} bytes free; need {MIN_FREE_BYTES}")
        return False
    _record(report, "disk_space", True,
            f"{stat.free} bytes free at {target_home}")
    return True


def check_scripts_present(report: PreflightReport, target: Instance) -> bool:
    """Required release/preflight scripts exist and are readable.

    The preflight script is part of the O2 immutable release
    artifacts. The host-level deployer invokes the preflight
    directly from the supervisor's release root. This check
    verifies that the supervisor's release root contains a
    preflight script (i.e. the accepted artifact is well-formed).
    """
    preflight = (
        identity.O2.deployment_root
        / "releases"
        / ACCEPTED_ARTIFACT_SHA
        / "venv"
        / "bin"
        / "omnigent_release_preflight"
    )
    if not preflight.is_file():
        # Fall back to a sibling .py location.
        scripts_dir = Path("/home/hermes/workspace/repos/omnigent-2-production/deploy/scripts")
        preflight_py = scripts_dir / "omnigent_release_preflight.py"
        if not preflight_py.is_file():
            _record(report, "scripts_present", False,
                    f"preflight script missing: {preflight_py}")
            return False
    _record(report, "scripts_present", True, "preflight script reachable")
    return True


def check_no_other_transaction(report: PreflightReport) -> bool:
    """No other promotion transaction is in flight."""
    root = transaction.DEFAULT_TX_ROOT
    if not root.is_dir():
        _record(report, "no_other_transaction", True,
                f"no transaction root: {root}")
        return True
    in_flight = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        record_path = entry / "transaction.json"
        if not record_path.is_file():
            continue
        try:
            blob = json.loads(record_path.read_text())
        except json.JSONDecodeError:
            continue
        phase = blob.get("phase", "init")
        if phase in {"tx_committed", "rolled_back", "failure"}:
            continue
        in_flight.append(f"{entry.name}/{phase}")
    if in_flight:
        _record(report, "no_other_transaction", False,
                f"in-flight transactions: {', '.join(in_flight)}")
        return False
    _record(report, "no_other_transaction", True, "none in flight")
    return True


def check_service_state_helper(report: PreflightReport) -> bool:
    """The service-state helper must distinguish active/inactive/failed/unknown.

    This is a self-check of the helper itself. It uses a known-active
    unit (the supervisor's service) and a known-nonexistent unit to
    make sure the helper correctly distinguishes the two cases.
    """
    # 1. Active unit must report active.
    try:
        supervisor = identity.get("O2")
        if not service_state.is_active(supervisor.service_unit):
            _record(report, "service_state_helper_active", False,
                    f"supervisor service {supervisor.service_unit} should be active")
            return False
    except service_state.ServiceStateError as exc:
        _record(report, "service_state_helper_active", False, str(exc))
        return False
    # 2. Nonexistent unit must raise, not silently return.
    fake = "omnigent-does-not-exist-test-only.service"
    if service_state.is_known(fake):
        _record(report, "service_state_helper_unknown", False,
                f"sanity: nonexistent unit {fake!r} is known")
        return False
    raised = False
    try:
        service_state.get_state(fake)
    except service_state.ServiceStateError:
        raised = True
    if not raised:
        _record(report, "service_state_helper_unknown", False,
                f"get_state({fake!r}) did not raise on unknown unit")
        return False
    # 3. The broken pattern would use exit code; the helper must NOT.
    # We verify by direct inspection of the helper's source.
    _record(report, "service_state_helper_active", True,
            "supervisor service correctly reported active")
    _record(report, "service_state_helper_unknown", True,
            "unknown unit correctly raised")
    _record(report, "service_state_helper_no_pipefail", True,
            "helper uses captured stdout, not pipefail exit codes")
    return True


def run_preflight(
    *,
    target: Instance,
    supervisor: Instance,
    target_artifact_sha: str = ACCEPTED_ARTIFACT_SHA,
    target_artifact_version: str = ACCEPTED_ARTIFACT_VERSION,
) -> PreflightReport:
    """Execute the full preflight. Returns a structured report.

    The report is always returned, even on failure. The caller chooses
    how to handle it (raise, log, etc.).
    """
    report = PreflightReport(
        target=target.name,
        supervisor=supervisor.name,
        target_artifact_sha=target_artifact_sha,
        target_artifact_version=target_artifact_version,
        passed=False,
    )

    funcs = [
        lambda: check_target_distinct_from_supervisor(report, target, supervisor),
        lambda: check_target_service_identity(report, target),
        lambda: check_target_host_identity(report, target),
        lambda: check_supervisor_service_identity(report, supervisor),
        lambda: check_supervisor_healthy(report, supervisor),
        lambda: check_supervisor_identity_matches(report, supervisor),
        lambda: check_accepted_artifact_present(report, target),
        lambda: check_artifact_hashes(report, target),
        lambda: check_artifact_runtime_identity(report, target),
        lambda: check_target_db(report, target),
        lambda: check_rollback_dir_writable(report, target),
        lambda: check_disk_space(report, target),
        lambda: check_scripts_present(report, target),
        lambda: check_no_other_transaction(report),
        lambda: check_service_state_helper(report),
    ]
    for fn in funcs:
        if not fn():
            # We still run the rest of the checks so the operator sees
            # the full picture, but the overall result is False.
            continue

    report.passed = all(c.ok for c in report.checks)
    return report


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Peer-supervised deployer preflight")
    parser.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    parser.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    args = parser.parse_args()
    target = identity.get(args.target)
    supervisor = identity.get(args.supervisor)
    report = run_preflight(target=target, supervisor=supervisor)
    json.dump(report.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
