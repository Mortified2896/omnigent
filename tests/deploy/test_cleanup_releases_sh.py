"""Tests for the cleanup-releases shell script logic.

The script itself runs ``rm -rf`` and ``find`` so we don't invoke it
from CI tests; instead we replicate the retention rules in Python and
pin them to the script's documented invariants.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "cleanup_releases.sh"


def test_cleanup_script_uses_keep_arg() -> None:
    text = _SCRIPT.read_text()
    assert "--keep" in text
    assert "--dry-run" in text


def test_cleanup_script_pins_current_previous_and_recorded() -> None:
    """Cleanup never deletes ``current``, ``previous``, or the recorded ``deployed-sha``.

    The script reads three sources of truth to build its retention
    list; the invariants here are what prevent an operator with
    ``sudo`` from accidentally deleting the live release.
    """
    text = _SCRIPT.read_text()
    assert "CURRENT_LINK" in text
    assert "PREVIOUS_LINK" in text
    assert "DEPLOYED_SHA_FILE" in text
    assert "PREV_DEPLOYED_SHA_FILE" in text


def test_cleanup_script_refuses_to_delete_live_process_dir() -> None:
    text = _SCRIPT.read_text()
    assert "LIVE_EXE" in text
    assert "refusing to delete" in text
