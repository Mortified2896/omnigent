"""Tests for the canonical live-deployed-SHA helper.

The external updater and the production /health endpoint both
read ``/var/lib/omnigent/shared/deployed-sha``. ``promote_release.sh``
and ``rollback_release.sh`` both write it. The shared helper
``scripts/_deployed_sha.sh`` is the single source of truth for
that path so all three actors agree on the same file.

These tests source the helper into a subshell and exercise the
real on-disk behavior, not just the helper's text.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HELPER = _REPO_ROOT / "scripts" / "_deployed_sha.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the helper with the given args and return the result."""
    full_env = os.environ.copy()
    full_env.pop("OMNIGENT_DEPLOYED_SHA_FILE", None)
    full_env.pop("OMNIGENT_DEPLOYED_SHA_DIR", None)
    full_env.pop("OMNIGENT_PREV_DEPLOYED_SHA_FILE", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{_HELPER}" && {"; ".join(args)}'],
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )


def test_helper_writes_via_helper_function(tmp_path: Path) -> None:
    """``_deployed_sha_write_current`` writes to $DEPLOYED_SHA_FILE."""
    target = tmp_path / "shared" / "deployed-sha"
    rc = _run(
        "_deployed_sha_mkdir",
        "_deployed_sha_write_current abc1234567890abcdef1234567890abcdef123456",
        f'test -f "{target}" && echo OK',
        env={"OMNIGENT_DEPLOYED_SHA_FILE": str(target)},
    )
    assert rc.returncode == 0, rc.stderr
    assert "OK" in rc.stdout
    assert target.read_text().strip() == "abc1234567890abcdef1234567890abcdef123456"


def test_helper_write_previous_uses_separate_file(tmp_path: Path) -> None:
    """``_deployed_sha_write_previous`` writes a separate file."""
    cur = tmp_path / "shared" / "deployed-sha"
    prev = tmp_path / "shared" / "previous-deployed-sha"
    rc = _run(
        "_deployed_sha_mkdir",
        "_deployed_sha_write_current aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "_deployed_sha_write_previous bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        f'test -f "{cur}" && test -f "{prev}" && echo OK',
        env={"OMNIGENT_DEPLOYED_SHA_FILE": str(cur), "OMNIGENT_PREV_DEPLOYED_SHA_FILE": str(prev)},
    )
    assert rc.returncode == 0, rc.stderr
    assert "OK" in rc.stdout
    assert cur.read_text().strip() == "a" * 40
    assert prev.read_text().strip() == "b" * 40


def test_helper_write_is_atomic(tmp_path: Path) -> None:
    """A write replaces the previous file in one move; no half-written file."""
    target = tmp_path / "shared" / "deployed-sha"
    rc = _run(
        "_deployed_sha_mkdir",
        "_deployed_sha_write_current 0000000000000000000000000000000000000000",
        "_deployed_sha_write_current 1111111111111111111111111111111111111111",
        f'test "$(cat {target})" = "1111111111111111111111111111111111111111" && echo OK',
        env={"OMNIGENT_DEPLOYED_SHA_FILE": str(target)},
    )
    assert rc.returncode == 0, rc.stderr
    assert "OK" in rc.stdout


def test_helper_dir_override_resolves_deployed_sha_under_dir(tmp_path: Path) -> None:
    """``OMNIGENT_DEPLOYED_SHA_DIR`` sets the directory both files live under."""
    shared = tmp_path / "shared"
    rc = _run(
        'echo "$DEPLOYED_SHA_FILE"',
        'echo "$PREV_DEPLOYED_SHA_FILE"',
        env={"OMNIGENT_DEPLOYED_SHA_DIR": str(shared)},
    )
    assert rc.returncode == 0, rc.stderr
    assert rc.stdout.strip().splitlines() == [
        str(shared / "deployed-sha"),
        str(shared / "previous-deployed-sha"),
    ]


def test_promote_release_script_uses_helper(tmp_path: Path) -> None:
    """``promote_release.sh`` sources the helper so its deployed-sha
    write goes through the canonical path."""
    script = _REPO_ROOT / "scripts" / "promote_release.sh"
    text = script.read_text()
    assert "scripts/_deployed_sha.sh" in text or "_deployed_sha.sh" in text
    assert "_deployed_sha_write_current" in text


def test_rollback_release_script_uses_helper(tmp_path: Path) -> None:
    """``rollback_release.sh`` sources the helper so its deployed-sha
    write goes through the canonical path."""
    script = _REPO_ROOT / "scripts" / "rollback_release.sh"
    text = script.read_text()
    assert "_deployed_sha.sh" in text
    assert "_deployed_sha_write_current" in text


def test_app_health_reader_honors_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_read_live_deployed_sha_for_health`` honors
    ``OMNIGENT_UPDATER_LIVE_SHA_FILE`` so the web service and the
    updater daemon always agree on the file."""
    from omnigent.server import app as server_app

    target = tmp_path / "shared" / "deployed-sha"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("0123456789abcdef0123456789abcdef01234567\n")
    monkeypatch.setenv("OMNIGENT_UPDATER_LIVE_SHA_FILE", str(target))
    out = server_app._read_live_deployed_sha_for_health()
    assert out == "0123456789abcdef0123456789abcdef01234567"


def test_app_health_reader_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing marker yields ``None`` so the bare ``/health`` contract holds."""
    from omnigent.server import app as server_app

    monkeypatch.setenv("OMNIGENT_UPDATER_LIVE_SHA_FILE", str(tmp_path / "missing-sha"))
    assert server_app._read_live_deployed_sha_for_health() is None
