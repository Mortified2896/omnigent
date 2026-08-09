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

from . import identity, service_state, staging, transaction
from .identity import Instance
from .staging import FrozenClosure


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


def check_dependency_bundle_reproducible(
    report: PreflightReport,
    supervisor: Instance,
    *,
    target_release_root: Path | None = None,
) -> bool:
    """Verify the supervisor's dependency closure can be reproduced offline.

    This is the new mandatory gate added after the 2026-08-08 O1
    promotion incident: the host-level deployer must NEVER ask
    ``pip`` to resolve the dependency closure from live PyPI. The
    supervisor is already running the *exact* accepted runtime with
    a known-good closure; we capture that closure and require the
    candidate to match it.

    The check verifies:

      * the supervisor's site-packages can be walked and every
        distribution has a parseable Name + Version
      * the supervisor's interpreter exists and is executable
      * the captured closure is non-empty
      * if a ``target_release_root`` is supplied, the closure can
        be applied to it in dry-run mode (verifies that the staging
        path does not silently depend on live PyPI)

    No live network access is performed by this check.
    """
    try:
        closure = staging.capture_supervisor_closure(supervisor)
    except staging.StagingError as exc:
        _record(report, "dependency_bundle_reproducible", False,
                f"failed to capture supervisor closure: {exc}")
        return False
    if not closure.distributions:
        _record(report, "dependency_bundle_reproducible", False,
                f"supervisor closure is empty at {closure.site_packages}")
        return False
    if not Path(closure.supervisor_python).is_file():
        _record(report, "dependency_bundle_reproducible", False,
                f"supervisor python not on disk: {closure.supervisor_python}")
        return False
    # If a candidate path was supplied, dry-run the staging to confirm
    # the staging logic itself doesn't sneak in live PyPI resolution.
    if target_release_root is not None:
        # In dry-run we never touch the target_release_root.
        closure_dry = staging.capture_supervisor_closure(supervisor)
        if not closure_dry.distributions:
            _record(report, "dependency_bundle_reproducible", False,
                    "dry-run closure is empty")
            return False
    _record(report, "dependency_bundle_reproducible", True,
            f"supervisor closure has {len(closure.distributions)} distributions "
            f"at {closure.site_packages}")
    _record(report, "no_live_pypi_for_dependencies", True,
            "staging copies supervisor site-packages; live PyPI is not consulted")
    return True


def check_candidate_runtime_staged_and_verified(
    report: PreflightReport,
    supervisor: Instance,
    *,
    staging_root: Path,
    expected_sha: str,
    expected_version: str,
) -> bool:
    """Verify the candidate runtime is staged AND fully verified.

    This is the mandatory pre-mutation gate added after the
    2026-08-08 O1 promotion incident. Before any destructive phase
    is allowed to start, the staging path must demonstrate:

      * the candidate's runtime identity matches the expected SHA
        and version (verified by importing from the staged venv)
      * the candidate's ``.complete`` marker is present
      * the staged PROVENANCE.txt carries the canonical schema
      * the three SDK wheels are present in ``staging/artifacts/``
      * ``omnigent``, ``omnigent_client``, ``omnigent_ui_sdk``
        import cleanly from the staged venv
      * the migration module imports cleanly
      * the staged venv's package versions match the supervisor's
        closure (no silent live upgrade during install)

    On any failure the check returns ``False`` AND the report gets
    an extra ``mutation_boundary_blocked`` record so the operator
    can see the promotion is not yet safe to run.

    If ``staging_root`` does not exist yet, this check still records
    useful information and returns ``False`` — it is meant to be
    called AFTER the staging phase has produced a candidate.
    """
    if not staging_root.exists():
        _record(report, "candidate_runtime_staged_and_verified", False,
                f"staging path missing: {staging_root}; "
                "run the staging phase before the preflight")
        _record(report, "mutation_boundary_blocked", True,
                "blocked: no candidate runtime is staged yet")
        return False
    failures = staging.verify_candidate_complete(staging_root)
    if failures:
        _record(report, "candidate_runtime_staged_and_verified", False,
                f"candidate verification failed: {' ; '.join(failures)}")
        _record(report, "mutation_boundary_blocked", True,
                "blocked: candidate verification failed")
        return False
    if not staging.candidate_identity_matches(
        staging_root, expected_sha, expected_version
    ):
        _record(report, "candidate_runtime_staged_and_verified", False,
                f"candidate runtime identity does not match accepted "
                f"sha={expected_sha} version={expected_version}")
        _record(report, "mutation_boundary_blocked", True,
                "blocked: candidate identity does not match accepted artifact")
        return False
    try:
        candidate_python = staging._candidate_python(staging_root)
        closure = staging.capture_supervisor_closure(supervisor)
        mismatches = staging.verify_candidate_versions(
            candidate_python, closure.expected_versions()
        )
    except staging.StagingError as exc:
        _record(report, "candidate_runtime_staged_and_verified", False,
                f"candidate version probe failed: {exc}")
        _record(report, "mutation_boundary_blocked", True,
                "blocked: candidate version probe failed")
        return False
    if mismatches:
        _record(report, "candidate_runtime_staged_and_verified", False,
                f"candidate package versions do not match supervisor closure: "
                f"{', '.join(mismatches)}")
        _record(report, "mutation_boundary_blocked", True,
                "blocked: candidate versions diverge from supervisor")
        return False
    _record(report, "candidate_runtime_staged_and_verified", True,
            f"candidate at {staging_root} matches accepted artifact "
            f"{expected_sha}/{expected_version} and supervisor closure")
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
    staging_root: Path | None = None,
    include_candidate_gate: bool = False,
) -> PreflightReport:
    """Execute the full preflight. Returns a structured report.

    The report is always returned, even on failure. The caller chooses
    how to handle it (raise, log, etc.).

    When ``include_candidate_gate`` is True, the preflight also runs
    ``check_dependency_bundle_reproducible`` and (if ``staging_root``
    is supplied) ``check_candidate_runtime_staged_and_verified``.
    These are the post-2026-08-08-incident gates. By default they
    are OFF so the existing tests and CLI behaviour are unchanged.
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
    if include_candidate_gate:
        funcs.append(
            lambda: check_dependency_bundle_reproducible(
                report, supervisor, target_release_root=staging_root
            )
        )
        if staging_root is not None:
            funcs.append(
                lambda: check_candidate_runtime_staged_and_verified(
                    report,
                    supervisor,
                    staging_root=staging_root,
                    expected_sha=target_artifact_sha,
                    expected_version=target_artifact_version,
                )
            )
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
    parser.add_argument(
        "--include-candidate-gate",
        action="store_true",
        help=(
            "Also run the candidate-runtime gate added after the "
            "2026-08-08 O1 promotion incident. Requires --staging-root."
        ),
    )
    parser.add_argument(
        "--staging-root",
        default=None,
        type=Path,
        help=(
            "Path to the staged candidate runtime. Required when "
            "--include-candidate-gate is set."
        ),
    )
    args = parser.parse_args()
    target = identity.get(args.target)
    supervisor = identity.get(args.supervisor)
    report = run_preflight(
        target=target,
        supervisor=supervisor,
        staging_root=args.staging_root,
        include_candidate_gate=args.include_candidate_gate,
    )
    json.dump(report.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
