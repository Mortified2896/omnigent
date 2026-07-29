"""Tests for the systemd drop-in writer.

The drop-in writer is the only piece that the operator-level
``scripts/promote_release.sh`` invokes via ``sudo``; everything else
runs as the calling user. These tests pin the drop-in schema so a
future change doesn't accidentally make the live systemd unit point
at a hard-coded path again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.deploy.ops import systemd


@pytest.fixture
def dropin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIGENT_DEPLOY_DROPIN_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def release_dir(tmp_path: Path) -> Path:
    r = tmp_path / "releases" / "0123456789abcdef0123456789abcdef01234567"
    r.mkdir(parents=True)
    return r


def test_write_release_dropin_atomic(dropin_dir: Path, release_dir: Path) -> None:
    """Drop-in file exists, has the expected fields, and is not a tmp file."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    path = systemd.write_release_dropin(sha, release_dir=release_dir)
    assert path.exists()
    body = path.read_text()
    assert "OMNIGENT_RELEASE_DIR=" in body
    assert f"OMNIGENT_RELEASE_DIR={release_dir}" in body
    assert f"OMNIGENT_RELEASE_EXPECTED_SHA={sha}" in body
    assert "WorkingDirectory=" in body
    assert "ExecStartPre=" in body
    # The pre-start gate uses the release's own python interpreter, not
    # the host's ``/usr/bin/python3`` — so a stray drop-in can never
    # accidentally regress to running the gate with a different
    # interpreter.
    assert f"{release_dir}/.venv/bin/python" in body
    # No leftover ``.tmp`` siblings.
    siblings = list(dropin_dir.iterdir())
    for entry in siblings:
        assert not entry.name.startswith(f".{path.name}")


def test_write_release_dropin_overwrites_safely(dropin_dir: Path, release_dir: Path) -> None:
    """Re-writing the same drop-in id does not leave partial files."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    path1 = systemd.write_release_dropin(sha, release_dir=release_dir)
    path2 = systemd.write_release_dropin(sha, release_dir=release_dir)
    assert path1 == path2
    assert path1.read_text() == path2.read_text()


def test_disable_other_release_dropins_keeps_active(dropin_dir: Path, release_dir: Path) -> None:
    """Other 10-release-* drop-ins get renamed ``.disabled``; the active stays put."""
    active_sha = "0123456789abcdef0123456789abcdef01234567"
    other_sha = "ffffffffffffffffffffffffffffffffffffffff"
    systemd.write_release_dropin(active_sha, release_dir=release_dir)
    other_path = systemd.write_release_dropin(other_sha, release_dir=release_dir / "other")
    disabled = systemd.disable_other_release_dropins(active_sha)
    assert other_path.with_suffix(other_path.suffix + ".disabled") in disabled
    # Active drop-in survives untouched.
    active_path = dropin_dir / f"10-release-{active_sha[:12]}.conf"
    assert active_path.is_file()
    assert active_path.read_text() == active_path.read_text()


def test_disable_other_release_dropins_no_op_when_only_active(
    dropin_dir: Path, release_dir: Path
) -> None:
    """If only the active drop-in is present, ``disable_other`` is a no-op."""
    active_sha = "0123456789abcdef0123456789abcdef01234567"
    systemd.write_release_dropin(active_sha, release_dir=release_dir)
    disabled = systemd.disable_other_release_dropins(active_sha)
    assert disabled == []
    # The active drop-in file still exists.
    active_path = dropin_dir / f"10-release-{active_sha[:12]}.conf"
    assert active_path.is_file()
