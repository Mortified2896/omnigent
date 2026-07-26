"""Tests for the deploy-main-* promotion shell script.

The script itself runs ``systemctl`` and ``npm`` so we can't fully
exercise it in CI, but we can pin the structural pieces that prevent
the silent-API-only regression: syntax validity, executable bit,
embedded steps, and the preflight invocation. Failing these tests
means the promotion script no longer wires a preflight gate before
reloading systemd — which is exactly the bug that produced the
deploy-main-0039e23a silent API-only deployment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "promote_main_deploy.sh"


def test_promote_script_exists() -> None:
    """The promotion script is shipped in the repository."""
    assert _SCRIPT_PATH.is_file(), f"missing {_SCRIPT_PATH}"


def test_promote_script_is_executable() -> None:
    """The promotion script has the executable bit set."""
    assert os.access(_SCRIPT_PATH, os.X_OK), f"{_SCRIPT_PATH} not executable"


def test_promote_script_parses_with_bash() -> None:
    """The script is syntactically valid bash.

    ``bash -n`` parses without executing; a syntax error here means the
    promotion will fail in production with a confusing message rather
    than the structured runbook the script prints.
    """
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


def test_promote_script_runs_preflight_before_restart() -> None:
    """The script invokes the preflight before the systemctl restart.

    This is the load-bearing gate: the bug we are fixing is exactly
    the case where the deploy script skipped the bundle build and
    restarted systemd, which then silently degraded to the API-only
    landing page. The order — preflight then restart — is the only
    thing that prevents that regression.
    """
    text = _SCRIPT_PATH.read_text()
    # Strip the leading docstring so the test does not match the comment
    # text which mentions both `systemctl restart` and the preflight in
    # prose. The script body starts after the closing ``set -euo pipefail``
    # line.
    body_marker = "set -euo pipefail"
    body_idx = text.find(body_marker)
    assert body_idx != -1, "expected `set -euo pipefail` marker in script"
    body = text[body_idx:]
    preflight_idx = body.find("omnigent.deploy.preflight")
    restart_idx = body.find("systemctl restart")
    assert preflight_idx != -1, "preflight invocation not found in script body"
    assert restart_idx != -1, "systemctl restart not found in script body"
    assert preflight_idx < restart_idx, (
        "preflight must run before the systemctl restart; the deploy-main-0039e23a "
        "silent API-only regression was caused by doing the restart without a "
        "preflight gate"
    )


def test_promote_script_runs_npm_build() -> None:
    """The script builds the frontend bundle as part of the promotion."""
    text = _SCRIPT_PATH.read_text()
    assert "npm run build" in text, "npm run build step missing"
    assert "npm ci" in text, "npm ci step missing (with npm install fallback)"


def test_promote_script_health_check_rejects_api_only_landing() -> None:
    """The post-restart health check refuses to mark the promotion successful
    if the server is serving the API-only landing page."""
    text = _SCRIPT_PATH.read_text()
    assert "OMNIGENT_SKIP_WEB_UI" in text, (
        "health check must grep for the API-only landing marker "
        "(OMNIGENT_SKIP_WEB_UI) so a silent-API-only deployment cannot slip through"
    )
    assert "<title>Omnigent</title>" in text, (
        "health check must verify the SPA shell is being served"
    )


def test_promote_script_disables_previous_drop_in() -> None:
    """The script disables the previous drop-in before activating the new one,
    so drop-in precedence resolves to the new deployment deterministically."""
    text = _SCRIPT_PATH.read_text()
    assert ".disabled" in text, "previous-drop-in disabling is missing"
    assert "10-deploy-main-*.conf" in text, "drop-in pattern not scoped to deploy-main-*"


def test_promote_script_writes_deployed_sha_path() -> None:
    """The script updates ``~/.omnigent/deployed-sha`` on success so the next
    session has a single source of truth for the live deployment SHA."""
    text = _SCRIPT_PATH.read_text()
    assert "deployed-sha" in text, "deployed-sha update is missing"


def test_promote_script_does_not_set_omnigent_skip_web_ui() -> None:
    """The script must NOT set ``OMNIGENT_SKIP_WEB_UI`` — the deploy site is a
    UI deployment, and silently opting it into API-only mode would defeat the
    point of the preflight."""
    text = _SCRIPT_PATH.read_text()
    # Allow the env var to be mentioned in a comment / docstring, but not
    # in any line that actually sets it.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        assert "OMNIGENT_SKIP_WEB_UI=" not in line, (
            f"the promotion script must not set OMNIGENT_SKIP_WEB_UI "
            f"(otherwise the preflight would be bypassed): {line!r}"
        )
