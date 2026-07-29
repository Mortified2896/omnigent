"""Tests for the deploy status and rollback shell scripts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATUS_SCRIPT = _REPO_ROOT / "scripts" / "deploy_status.sh"
_ROLLBACK_SCRIPT = _REPO_ROOT / "scripts" / "rollback_release.sh"
_CLEANUP_SCRIPT = _REPO_ROOT / "scripts" / "cleanup_releases.sh"


@pytest.mark.parametrize("script", [_STATUS_SCRIPT, _ROLLBACK_SCRIPT, _CLEANUP_SCRIPT])
def test_script_exists_and_executable(script: Path) -> None:
    assert script.is_file()
    assert os.access(script, os.X_OK)


@pytest.mark.parametrize("script", [_STATUS_SCRIPT, _ROLLBACK_SCRIPT, _CLEANUP_SCRIPT])
def test_script_parses_with_bash(script: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    proc = subprocess.run(
        [bash, "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_status_script_uses_release_symlinks() -> None:
    """Status reads ``current`` and ``previous`` symlinks to identify releases."""
    text = _STATUS_SCRIPT.read_text()
    assert "DEPLOY_ROOT" in text
    assert "CURRENT_LINK" in text
    assert "PREVIOUS_LINK" in text


def test_status_script_reports_provenance() -> None:
    """Status command surfaces live PID, executable, and module path."""
    text = _STATUS_SCRIPT.read_text()
    assert "live_pid" in text
    assert "live_exe" in text
    assert "live_omnigent_module" in text
    assert "STATUS:" in text


def test_status_script_exits_nonzero_on_mismatch() -> None:
    """Exit codes from the status command are 0=OK, 1=MISMATCH, 2=FATAL."""
    text = _STATUS_SCRIPT.read_text()
    assert "exit 1" in text
    assert "exit 2" in text


def test_rollback_script_reads_previous_symlink() -> None:
    """Rollback falls back to ``previous`` symlink when no ``--to`` is supplied."""
    text = _ROLLBACK_SCRIPT.read_text()
    assert "PREVIOUS_LINK" in text
    assert "--to" in text
    assert "systemctl restart" in text


def test_rollback_script_waits_for_active_state() -> None:
    """The rollback waits up to ~60s for the service to come back active."""
    text = _ROLLBACK_SCRIPT.read_text()
    assert "is-active" in text
    # The polling loop is bounded; a runaway loop would only fail at the
    # very end of CI. We assert the time budget explicitly.
    assert "seq 1 30" in text


def test_rollback_script_refuses_to_skip_loopback_probe() -> None:
    """Rollback requires /health and ``/`` to succeed before committing the SHA."""
    text = _ROLLBACK_SCRIPT.read_text()
    assert "/health" in text
    assert "OMNIGENT_SKIP_WEB_UI" in text


def test_cleanup_script_keeps_current_and_previous() -> None:
    """Cleanup pins current and previous releases unconditionally."""
    text = _CLEANUP_SCRIPT.read_text()
    assert "CURRENT_LINK" in text
    assert "PREVIOUS_LINK" in text
    assert "DEPLOY_ROOT" in text


def test_cleanup_script_refuses_to_delete_live_release() -> None:
    """The live service's executable path is excluded from the deletion set."""
    text = _CLEANUP_SCRIPT.read_text()
    assert "LIVE_EXE" in text
    assert "refusing to delete" in text
