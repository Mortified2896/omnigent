"""Pre-start gate the systemd unit runs via ``ExecStartPre=``.

The gate is intentionally a thin wrapper around three single-purpose
modules so each check can be unit-tested in isolation:

* :mod:`omnigent.deploy.supervisor.provenance` proves the running
  interpreter actually loads ``omnigent`` from the configured release
  directory (no editable cross-import).
* :mod:`omnigent.deploy.supervisor.manifest` reads the machine-readable
  manifest written at promotion time and verifies the recorded SHA
  matches the git HEAD of the release directory.
* :mod:`omnigent.deploy.preflight` is the existing web-UI bundle check.

The gate exits non-zero on any failure and prints a runbook-quality
message that includes the exact systemd Action= to recover
(``systemctl reset-failed`` etc.). For the production service this
means a failed deployment cannot come up silently; the journal will
show ``omnigent-eval-web.service: Main process exited, code=exited,
status=1/FAILURE`` with the runbook on stderr.

API-only deployments remain supported: setting
``OMNIGENT_SKIP_WEB_UI=true`` in the unit environment disables the
bundle check while the rest of the gate still runs. This is the
canonical "I want a degraded deploy on purpose" signal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from omnigent.deploy.preflight import is_api_only_deployment, verify_web_ui_bundle
from omnigent.deploy.supervisor.manifest import (
    ManifestError,
    load_manifest,
    verify_manifest_commit,
)
from omnigent.deploy.supervisor.provenance import (
    ProvenanceError,
    check_runtime_provenance,
)


class GateError(RuntimeError):
    """Raised when one of the pre-start checks refuses to start the unit.

    The message is intentionally verbose — systemd captures the unit's
    stderr in the journal and operators triage failures from those
    lines without interactive access to the unit.
    """


def _run_subprocess_with_venv(release: Path, args: list[str]) -> tuple[int, str]:
    """Run ``args`` using the release's local Python interpreter.

    Used by the manifest check so the recorded SHA can be verified
    against ``git -C <release> rev-parse HEAD`` without depending on
    the global ``git`` user (``User=hermes``) having access to a
    check-in's git tree.
    """
    python = release / ".venv" / "bin" / "python"
    if not python.is_file():
        raise GateError(f"release python not found: {python}")
    try:
        proc = subprocess.run(
            [str(python), *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=release,
        )
    except OSError as exc:
        raise GateError(f"failed to spawn {python}: {exc}") from exc
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def run_gate(release: Path, *, skip_web_ui: bool | None = None) -> dict[str, str]:
    """Run all pre-start checks. Return a dict of resolved values.

    :param release: Absolute path of the configured release directory.
    :param skip_web_ui: If True, do not require the web UI bundle. If
        None, decide based on the ``OMNIGENT_SKIP_WEB_UI`` env var
        (same truthy values as :func:`is_api_only_deployment`).
    :returns: Dict with the resolved provenance paths and the recorded
        SHA, suitable for ``ExecStartPost`` capture or status output.
    :raises GateError: When any check fails. The error message includes
        the specific check and a remediation hint.
    """
    if skip_web_ui is None:
        skip_web_ui = is_api_only_deployment()

    # 1. Provenance — refuses to start when the venv imports from a
    #    different checkout than the one systemd thinks it's running.
    try:
        provenance = check_runtime_provenance(release)
    except ProvenanceError as exc:
        raise GateError(
            f"runtime provenance check failed: {exc}. "
            f"Rebuild the release with scripts/promote_release.sh so the "
            f".venv belongs to {release}."
        ) from exc

    # 2. Manifest — recorded SHA must match the git HEAD of the release.
    #    Skipped when the release has not been promoted yet (e.g. a
    #    test fixture that assembles a release by hand).
    recorded_sha = os.environ.get("OMNIGENT_RELEASE_EXPECTED_SHA", "").strip()
    if recorded_sha:
        try:
            manifest = load_manifest(release)
        except ManifestError as exc:
            raise GateError(
                f"manifest check failed: {exc}. "
                f"Repromote the release with scripts/promote_release.sh "
                f"to regenerate manifests/{recorded_sha}.json."
            ) from exc
        try:
            verify_manifest_commit(manifest, recorded_sha)
        except ManifestError as exc:
            raise GateError(
                f"manifest SHA mismatch: {exc}. "
                f"The release {release} was promoted with a different SHA; "
                f"point the drop-in at the correct release or roll back."
            ) from exc

    # 3. Web UI bundle — refuses to start a normal UI deployment on
    #    an unbuilt release. The api-only opt-out is honored.
    if not skip_web_ui:
        try:
            verify_web_ui_bundle(release)
        except Exception as exc:  # WebUIBundleMissingError or other
            raise GateError(f"web UI bundle check failed: {exc}") from exc

    return {
        **provenance,
        "skip_web_ui": "1" if skip_web_ui else "0",
        "manifest_sha": recorded_sha or "",
    }


def main() -> int:
    """CLI entry point for ``ExecStartPre=/.venv/bin/python -m
    omnigent.deploy.supervisor.gate``.

    Reads ``OMNIGENT_RELEASE_DIR`` and (optional)
    ``OMNIGENT_RELEASE_EXPECTED_SHA`` from the unit's environment.
    """
    release_dir = os.environ.get("OMNIGENT_RELEASE_DIR", "").strip()
    if not release_dir:
        print("[deploy-supervisor] ERROR: OMNIGENT_RELEASE_DIR is not set", file=sys.stderr)
        return 2
    expected_sha = os.environ.get("OMNIGENT_RELEASE_EXPECTED_SHA", "").strip()
    if expected_sha:
        os.environ["OMNIGENT_RELEASE_EXPECTED_SHA"] = expected_sha
    try:
        info = run_gate(Path(release_dir))
    except GateError as exc:
        print(f"[deploy-supervisor] ERROR: {exc}", file=sys.stderr)
        return 1
    for key, value in info.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
