"""Behavioral tests for ``scripts/promote_release.sh`` provenance
invocation after the fix.

These tests pin the canonical promotion path's provenance invocation:

* the probe runs with the release's ``.venv/bin/python``;
* the probe runs from a neutral directory (``/tmp``);
* the probe has PYTHONPATH unset and PYTHONSAFEPATH=1 set;
* the probe uses Python's ``-P`` (--no-path) flag;
* a failed probe prevents promotion;
* the previous release remains active when candidate validation fails.

The previous tests pinned literal strings (``release =
pathlib.Path('$RELEASE_DIR').resolve()``) that the real canonical
invocation does not contain, because the canonical invocation now
delegates path resolution to the Python module. These tests replace
that brittle approach with behavioral assertions on the actual shell
plumbing.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "promote_release.sh"


def _extract_probe_block(text: str) -> str:
    """Return the substring around the canonical provenance probe."""
    body = text[text.find("set -euo pipefail") :]
    start = body.find('log "proving import provenance')
    if start == -1:
        return ""
    # Take everything from start to the next ``fail`` call.
    return body[start : body.find("fail ", start)]


def test_probe_uses_release_venv_python() -> None:
    """The probe invocation must use ``$RELEASE_DIR/.venv/bin/python``."""
    text = _SCRIPT_PATH.read_text()
    block = _extract_probe_block(text)
    assert '"$RELEASE_DIR/.venv/bin/python"' in block
    # And NOT the repo's python (which would import from the repo).
    assert "$REPO_ROOT/.venv/bin/python" not in block


def test_probe_runs_from_neutral_directory() -> None:
    """The probe must change to a neutral directory (e.g. ``/tmp``) before invoking Python."""
    text = _SCRIPT_PATH.read_text()
    block = _extract_probe_block(text)
    assert "cd /tmp" in block
    # The cd must be a separate command BEFORE the Python invocation.
    cd_idx = block.find("cd /tmp")
    py_idx = block.find(".venv/bin/python")
    assert cd_idx != -1 and py_idx != -1
    assert cd_idx < py_idx


def test_probe_unsets_pythonpath_and_uses_pysafepath() -> None:
    """The probe invocation must unset PYTHONPATH and set PYTHONSAFEPATH=1."""
    text = _SCRIPT_PATH.read_text()
    block = _extract_probe_block(text)
    assert "env -u PYTHONPATH" in block
    assert "PYTHONSAFEPATH=1" in block


def test_probe_uses_dash_p_flag() -> None:
    """The probe must use Python's ``-P`` (``--no-path``) flag so cwd/PYTHONPATH
    cannot shadow site-packages.
    """
    text = _SCRIPT_PATH.read_text()
    block = _extract_probe_block(text)
    # Match ``-P `` (with a trailing space) so we don't accidentally
    # accept ``-Pip`` or similar.
    assert re.search(r"-P\s", block), (
        f"promote_release.sh probe must invoke python with -P; block:\n{block}"
    )


def test_probe_runs_module_main() -> None:
    """The probe runs ``python -P -m omnigent.deploy.supervisor.provenance``."""
    text = _SCRIPT_PATH.read_text()
    block = _extract_probe_block(text)
    assert "-m omnigent.deploy.supervisor.provenance" in block


def test_probe_runs_before_systemd_reconfiguration() -> None:
    """The probe must run before any systemd drop-in write or restart."""
    text = _SCRIPT_PATH.read_text()
    body = text[text.find("set -euo pipefail") :]
    probe_idx = body.find("proving import provenance")
    dropin_idx = body.find("write_release_dropin")
    restart_idx = body.find("systemctl restart")
    assert probe_idx != -1
    assert dropin_idx != -1
    assert restart_idx != -1
    assert probe_idx < dropin_idx < restart_idx, (
        "provenance probe must run BEFORE the drop-in rewrite and the "
        "service restart (otherwise a failed probe leaves the service in "
        "an in-progress state)."
    )


def test_failed_probe_cleanups_release() -> None:
    """When the probe fails, the canonical script removes the failed release."""
    text = _SCRIPT_PATH.read_text()
    body = text[text.find("set -euo pipefail") :]
    block = _extract_probe_block(text)
    # Find the failure cleanup that follows the probe.
    after = body[body.find(block) :]
    # The cleanup should ``rm -rf "$RELEASE_DIR"`` and ``fail ...``.
    assert 'rm -rf "$RELEASE_DIR"' in after
    assert 'fail "import provenance check failed' in after


def test_failed_probe_keeps_previous_release_active() -> None:
    """When the candidate build fails, the script never touches ``current`` or ``previous``."""
    text = _SCRIPT_PATH.read_text()
    body = text[text.find("set -euo pipefail") :]
    # The "rotating symlinks and writing systemd drop-in" section is
    # the only place current/previous are written; it must come AFTER
    # the probe.
    probe_idx = body.find("proving import provenance")
    rotate_idx = body.find("rotating symlinks and writing systemd drop-in")
    assert probe_idx != -1 and rotate_idx != -1
    assert probe_idx < rotate_idx, (
        "symlink rotation must happen AFTER the provenance probe so a "
        "failed candidate cannot break the current release."
    )


def test_no_repo_path_insertion_in_probe() -> None:
    """The probe must not insert REPO_ROOT into sys.path; the Python
    module handles path resolution internally and would be confused
    by a shadowed ``omnigent`` package.
    """
    text = _SCRIPT_PATH.read_text()
    block = _extract_probe_block(text)
    assert "sys.path.insert" not in block
    assert "PYTHONPATH=$REPO_ROOT" not in block
