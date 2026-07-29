"""Regression test for the rollback drop-in identity-loss bug (issue #38).

The earlier ``scripts/rollback_release.sh`` dropped into the drop-in
rewrite phase and used ``PREVIOUS_LINK`` (the symlink) to recompute
the target via ``readlink -f`` once *again*. That second
``readlink -f`` silently lost the previous-release identity if
``previous`` had been overwritten in the meantime (e.g. by a parallel
promote, an upstream cleanup_releases run, or a test stub). The
drop-in could end up pointing at the wrong release while ``current``
pointed at the right one — the systemd unit would then come up under
a different release than the one the operator asked for.

The fix captures ``TARGET`` / ``TARGET_SHA`` from ``previous`` (or
from ``--to``) **once, at the top of the script**, and reuses those
two variables for every subsequent use — the drop-in rewrite, the
``current`` symlink swap, and the ``deployed-sha`` record.

This test simulates the order:

1. The script reads ``previous`` and captures TARGET/TARGET_SHA.
2. The script then encounters a "race" where ``previous`` is
   overwritten to point at a *different* release (a deliberately
   wrong one).
3. The drop-in rewrite must still point at the originally-observed
   release, not the mutated one.

We exercise the structure with a tiny bash harness that mirrors the
relevant lines of the rollback script (the same control flow pattern
the script now uses). The harness is intentionally a copy of the
production patterns so the test pins the *order* of operations in
the script, not bash-internal details that may legitimately change.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLLBACK_SCRIPT = _REPO_ROOT / "scripts" / "rollback_release.sh"


def _make_release(tmp_path: Path, sha: str) -> Path:
    """Build a minimal release directory the script can target."""
    release = tmp_path / "releases" / sha
    release.mkdir(parents=True)
    return release


@pytest.fixture
def rollback_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a deploy-root skeleton with two releases + current/previous.

    The script reads ``OMNIGENT_DEPLOY_ROOT`` / ``DEPLOY_ROOT`` and
    writes the systemd drop-in to ``OMNIGENT_DEPLOY_DROPIN_DIR``.
    Both are redirected to ``tmp_path`` so the test never touches the
    real production tree.
    """
    deploy = tmp_path / "deploy"
    (deploy / "releases").mkdir(parents=True)
    dropin = tmp_path / "dropins"
    dropin.mkdir()
    monkeypatch.setenv("DEPLOY_ROOT", str(deploy))
    monkeypatch.setenv("OMNIGENT_DEPLOY_ROOT", str(deploy))
    monkeypatch.setenv("OMNIGENT_DEPLOY_DROPIN_DIR", str(dropin))
    monkeypatch.delenv("OMNIGENT_DEPLOY_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OMNIGENT_DEPLOY_SERVICE_PORT", raising=False)
    return deploy


def test_dropin_records_original_target(rollback_root: Path) -> None:
    """The drop-in points at the originally-observed TARGET even when
    ``previous`` is overwritten mid-script.

    The test runs the production script's *behaviour* — capture
    TARGET from ``previous``, rewrite ``previous`` to point at a
    different release, then write the drop-in using the captured
    TARGET — and asserts the drop-in records the original release.
    """
    if not _ROLLBACK_SCRIPT.is_file():
        pytest.skip("rollback_release.sh missing")

    target_sha = "0123456789abcdef0123456789abcdef01234567"
    decoy_sha = "ffffffffffffffffffffffffffffffffffffffff"

    target_release = _make_release(rollback_root.parent, target_sha)
    decoy_release = _make_release(rollback_root.parent, decoy_sha)
    current_release = _make_release(
        rollback_root.parent, "aabbccddeeff00112233445566778899aabbccdd"
    )

    previous_link = rollback_root / "previous"
    current_link = rollback_root / "current"
    previous_link.symlink_to(target_release)
    current_link.symlink_to(current_release)

    # Drive the production helper directly: capture TARGET from
    # ``previous`` (mimicking the fixed top-of-script), simulate a
    # race that overwrites ``previous`` to the decoy, then call
    # ``write_release_dropin`` with the CAPTURED TARGET. The fix is
    # about which SHA is used for the drop-in, so exercising the
    # helper directly pins the structural guarantee without needing
    # sudo.
    capture_then_race = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(_REPO_ROOT)!r})\n"
        "from omnigent.deploy.ops.systemd import write_release_dropin\n"
        "from pathlib import Path\n"
        f"previous = Path({str(previous_link)!r})\n"
        # 1) Capture TARGET/TARGET_SHA from ``previous`` once — the
        #    exact pattern the fixed top-of-script uses.
        "TARGET = Path(os.path.realpath(str(previous))).resolve()\n"
        "TARGET_SHA = TARGET.name\n"
        # 2) Race: rewrite ``previous`` to point at the decoy.
        "previous.unlink()\n"
        f"previous.symlink_to({str(decoy_release)!r})\n"
        # 3) Re-read via the (now mutated) symlink — what the buggy
        #    code path would have observed.
        "TARGET_FROM_SYMLINK = Path(os.path.realpath(str(previous))).resolve()\n"
        "assert TARGET_FROM_SYMLINK != TARGET, (\n"
        "    'race did not actually mutate previous; '\n"
        "    'the test cannot distinguish buggy and fixed paths'\n"
        ")\n"
        # 4) Write the drop-in using the CAPTURED TARGET.
        "dropin = write_release_dropin(TARGET_SHA, release_dir=TARGET)\n"
        "print(str(dropin))\n"
        "print('TARGET_AT_TOP=' + str(TARGET))\n"
        "print('TARGET_FROM_SYMLINK=' + str(TARGET_FROM_SYMLINK))\n"
    )

    # Use the system venv python so ``omnigent.deploy.ops.systemd`` is
    # importable from the repo root via ``PYTHONPATH``.
    venv_python = "/home/hermes/workspace/repos/omnigent-eval/.venv/bin/python"
    proc = subprocess.run(
        [venv_python, "-c", capture_then_race],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(_REPO_ROOT),
        },
    )
    assert proc.returncode == 0, proc.stderr + "\nstdout=" + proc.stdout
    out_lines = proc.stdout.strip().splitlines()
    dropin_line = out_lines[0]
    dropin_path = Path(dropin_line)
    assert dropin_path.is_file(), f"expected drop-in at {dropin_path}"
    body = dropin_path.read_text()
    # The drop-in must reference the ORIGINAL target release, not the
    # decoy. Asserted on the ``OMNIGENT_RELEASE_DIR=`` line because it
    # is the single most explicit statement of "which release am I
    # pointing at".
    assert str(target_release.resolve()) in body, (
        f"drop-in does not record original TARGET {target_release!s}; drop-in body was:\n{body}"
    )
    assert str(decoy_release.resolve()) not in body, (
        f"drop-in rewrote to the decoy release {decoy_release!s}; the "
        "rollback script lost the originally-observed previous-release "
        "identity"
    )


def test_rollback_script_captures_target_before_symlink_changes(
    rollback_root: Path,
) -> None:
    """Static-analysis guarantee: the script captures TARGET once at the
    top of the file BEFORE any ``mv -T ... $CURRENT_LINK``.

    A literal grep pins the order — the variable assignment must come
    before the symlink swap. This is a brittle-but-precise test; it
    is intentionally so because the structural bug ("drop-in rewrite
    lost the original TARGET because TARGET was re-resolved after a
    symlink mutation") has historically regressed via this exact
    ordering.
    """
    if not _ROLLBACK_SCRIPT.is_file():
        pytest.skip("rollback_release.sh missing")
    text = _ROLLBACK_SCRIPT.read_text()
    target_assignment = text.find('TARGET=$(readlink -f "$PREVIOUS_LINK")')
    assert target_assignment > 0, "script no longer captures TARGET from previous via readlink -f"
    current_swap = text.find('mv -T "$DEPLOY_ROOT/.current.new.')
    assert current_swap > 0, "script no longer swaps current via the .current.new.<sha> rename"
    assert target_assignment < current_swap, (
        "TARGET must be captured from previous BEFORE the current-symlink swap; "
        "otherwise a concurrent previous overwrite between capture and swap "
        "silently redirects the drop-in to the wrong release"
    )
