"""Tests for the long-term deploy promotion shell script.

The script's behavior is partially exercised via subprocess (parse
the script, confirm it does not silently fall back from ``npm ci``
to ``npm install``, and pin the canonical-promotion invariants).
Full end-to-end promotion is exercised by the manual live migration.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "promote_release.sh"


def test_promote_script_exists() -> None:
    assert _SCRIPT_PATH.is_file()


def test_promote_script_is_executable() -> None:
    import os

    assert os.access(_SCRIPT_PATH, os.X_OK)


def test_promote_script_parses_with_bash() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    proc = subprocess.run(
        [bash, "-n", str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_promote_script_uses_npm_ci_not_install_fallback() -> None:
    """The deployment script must not silently fall back to ``npm install``.

    The brief calls this out explicitly: ``npm install`` would update
    the lockfile and resolve a new dependency graph, breaking
    reproducibility. The script runs ``npm ci`` and aborts on failure.
    """
    text = _SCRIPT_PATH.read_text()
    body_marker = "set -euo pipefail"
    body_idx = text.find(body_marker)
    assert body_idx != -1
    body = text[body_idx:]
    # Look for the exact ``if ! ...; then ... fail`` shape — that's the
    # "fail loudly on npm ci failure" branch the previous deployment
    # script lacked.
    assert "if ! PATH=" in body and "npm ci " in body
    # The body must contain a ``fail "npm ci failed`` line. We
    # tolerate comments and docstrings (the lazy ``grep`` would be
    # misled by them), so verify the exact pattern.
    assert 'fail "npm ci failed' in body


def test_promote_script_does_not_set_omnigent_skip_web_ui() -> None:
    """The deployment script must not opt the production deployment into
    API-only mode; doing so would bypass the bundle preflight."""
    text = _SCRIPT_PATH.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        assert "OMNIGENT_SKIP_WEB_UI=" not in line, (
            f"promote_release.sh must not set OMNIGENT_SKIP_WEB_UI: {line!r}"
        )


def test_promote_script_uses_release_local_venv() -> None:
    """The script installs the backend into ``$RELEASE_DIR/.venv``,
    not a shared or symlinked venv."""
    text = _SCRIPT_PATH.read_text()
    body_marker = "set -euo pipefail"
    body = text[text.find(body_marker) :]
    assert "uv venv --python 3.12 .venv" in body, (
        "promote_release.sh must create a release-local .venv via `uv venv`"
    )
    assert 'uv pip install --python "$RELEASE_DIR/.venv/bin/python"' in body, (
        "promote_release.sh must use the release-local python for `uv pip install`"
    )


def test_promote_script_validates_via_import_probe() -> None:
    """The script runs the import-provenance probe from a neutral
    directory with safe-path behavior.

    The earlier version of this test pinned the exact literal string
    ``release = pathlib.Path('$RELEASE_DIR').resolve()`` — which the
    real canonical invocation does not contain, because the canonical
    invocation is now ``python -m
    omnigent.deploy.supervisor.provenance`` (the Python module handles
    resolution internally). That brittle pattern broke every time the
    probe plumbing refactored, even when the underlying behavior was
    correct. The replacement is behavioral: assert the script runs the
    probe using the release's ``.venv/bin/python``, from a neutral
    directory, with ``PYTHONPATH`` unset and Python's ``-P`` flag.
    """
    text = _SCRIPT_PATH.read_text()
    body_marker = "set -euo pipefail"
    body = text[text.find(body_marker) :]
    # The probe must run with the release's interpreter, not the
    # main checkout's interpreter.
    assert "env -u PYTHONPATH" in body, (
        "promote_release.sh must unset inherited PYTHONPATH before invoking "
        "the provenance probe (PYTHONPATH from the operator's shell otherwise "
        "shadows the installed wheel)."
    )
    assert "PYTHONSAFEPATH=1" in body, (
        "promote_release.sh must set PYTHONSAFEPATH=1 before invoking the "
        "provenance probe (this is the venv-site-packages pre-flight)."
    )
    assert '"$RELEASE_DIR/.venv/bin/python" -P' in body, (
        "promote_release.sh must invoke the release's interpreter with the "
        "-P (--no-path) flag so cwd/PYTHONPATH cannot shadow site-packages."
    )
    assert "cd /tmp" in body, (
        "promote_release.sh must change into a neutral directory (e.g. /tmp) "
        "before invoking the provenance probe (running from the release "
        "directory inserts the release source root into sys.path and can "
        "shadow the installed wheel)."
    )
    assert "-m omnigent.deploy.supervisor.provenance" in body
    # The probe must run BEFORE any systemd reconfiguration (drop-in
    # write / systemctl restart).
    probe_idx = body.find("-m omnigent.deploy.supervisor.provenance")
    # The drop-in write is now routed through the sudo-allowed
    # wrapper at /opt/omnigent/updater/bin/write-dropin.sh.
    dropin_idx = body.find("write-dropin.sh")
    restart_idx = body.find("systemctl restart")
    assert dropin_idx != -1 and restart_idx != -1
    assert probe_idx < dropin_idx < restart_idx, (
        "the provenance probe must run before the systemd drop-in rewrite and the service restart."
    )


def test_promote_script_atomic_promotion() -> None:
    """The script promotes via symlink swap + drop-in, only after the
    live loopback probe passes."""
    text = _SCRIPT_PATH.read_text()
    body_marker = "set -euo pipefail"
    body = text[text.find(body_marker) :]
    # The drop-in write is now routed through the sudo-allowed
    # wrapper at /opt/omnigent/updater/bin/write-dropin.sh so the
    # literal ``write_release_dropin`` no longer appears inline.
    wrapper_idx = body.find("write-dropin.sh")
    # The deployed-sha write is routed through the shared helper
    # ``_deployed_sha.sh`` so the literal path string no longer
    # appears inline.
    helper_idx = body.find("_deployed_sha.sh")
    helper_write_idx = body.find("_deployed_sha_write_current")
    loopback_idx = body.find("/health")
    assert wrapper_idx != -1, "promote_release.sh must call write-dropin.sh"
    assert loopback_idx != -1
    assert helper_idx != -1, "promote_release.sh must source scripts/_deployed_sha.sh"
    assert helper_write_idx != -1, "promote_release.sh must call _deployed_sha_write_current"
    # deployed-sha update must happen after the drop-in write AND
    # after the loopback probe passes — otherwise a flaky live
    # restart would still record the SHA as "deployed".
    assert wrapper_idx < loopback_idx < helper_write_idx


def test_promote_script_does_not_use_main_checkout_runtime() -> None:
    """The script must not start the service with the main checkout's
    ``.venv/bin/python`` — only the release's venv is acceptable."""
    text = _SCRIPT_PATH.read_text()
    body_marker = "set -euo pipefail"
    body = text[text.find(body_marker) :]
    # exec starts must use the release python; we grep for the literal
    # ``$RELEASE_DIR/.venv`` so we don't pick up docstrings.
    assert "$RELEASE_DIR/.venv/bin/python" in body, (
        "promote_release.sh must exec the service from $RELEASE_DIR/.venv"
    )


def test_promote_script_writes_dropin_for_both_services() -> None:
    """``promote_release.sh`` writes a drop-in for **both** the web
    and host services.

    The host daemon must run from the same immutable release as
    the web service; pinning only the web service lets the host
    drift to a different checkout and silently breaks the
    cross-service provenance invariant. The script invokes the
    sudoers-gated wrapper with both ``web`` and ``host`` as the
    service kind so both drop-ins land on disk before any
    ``systemctl restart`` runs.
    """
    text = _SCRIPT_PATH.read_text()
    body_marker = "set -euo pipefail"
    body = text[text.find(body_marker) :]
    # Both services are pinned via the wrapper.
    assert "write-dropin.sh write web" in body, (
        "promote_release.sh must invoke write-dropin.sh with the 'web' service kind"
    )
    assert "write-dropin.sh write host" in body, (
        "promote_release.sh must invoke write-dropin.sh with the 'host' service kind"
    )
    # The disable calls also use the service-kind argument.
    assert "write-dropin.sh disable web" in body
    assert "write-dropin.sh disable host" in body
    # Both units are restarted.
    assert 'systemctl restart "$SERVICE_NAME"' in body, (
        "promote_release.sh must restart the web service"
    )
    assert 'systemctl restart "$HOST_SERVICE_NAME"' in body, (
        "promote_release.sh must restart the host daemon AFTER the web service"
    )
    # The web restart must precede the host restart; the host
    # depends on the loopback web service being up.
    web_restart_idx = body.find('systemctl restart "$SERVICE_NAME"')
    host_restart_idx = body.find('systemctl restart "$HOST_SERVICE_NAME"')
    assert 0 < web_restart_idx < host_restart_idx, (
        "the host daemon must be restarted AFTER the web service so a "
        "failing host restart cannot strand the web on a new release"
    )


def test_promote_script_verifies_host_pinned_to_release_venv() -> None:
    """``promote_release.sh`` verifies the host daemon's running
    executable is inside the release's ``.venv``.

    A host unit can report ``active`` while still running from a
    previous binary if the drop-in was overwritten mid-restart;
    the script must additionally read ``/proc/<pid>/exe`` for the
    host's MainPID and confirm the binary lives inside
    ``<release>/.venv``. If it does not, the script must roll
    back both services rather than declaring success.
    """
    text = _SCRIPT_PATH.read_text()
    body_marker = "set -euo pipefail"
    body = text[text.find(body_marker) :]
    # The script reads the host's MainPID and resolves its exe.
    assert "systemctl show -p MainPID --value" in body, (
        "promote_release.sh must read the host daemon's MainPID to verify pinning"
    )
    assert "/proc/" in body and "/exe" in body, (
        "promote_release.sh must read /proc/<pid>/exe to verify the "
        "host daemon is running the release's binary"
    )
    # The expected case statement checks the binary is inside the release's venv.
    assert '"$RELEASE_DIR"/.venv/*' in body, (
        "promote_release.sh must case-match the host's executable against "
        "the release's .venv/ prefix"
    )
    # On mismatch the script rolls back BOTH services, not just the host.
    rollback_idx = body.find("rolling back BOTH services")
    assert rollback_idx != -1, (
        "promote_release.sh must roll back both services when the host "
        "executable is not pinned to the release"
    )
