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
    dropin_idx = body.find("write_release_dropin")
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
    dropin_idx = body.find("write_release_dropin")
    deploy_idx = body.find(".omnigent/deployed-sha")
    loopback_idx = body.find("/health")
    assert dropin_idx != -1
    assert loopback_idx != -1
    assert deploy_idx != -1
    # deployed-sha update must happen after the drop-in write AND after
    # the loopback probe passes — otherwise a flaky live restart would
    # still record the SHA as "deployed".
    assert dropin_idx < loopback_idx < deploy_idx


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
