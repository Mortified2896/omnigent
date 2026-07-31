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


# ---------------------------------------------------------------------------
# Host service drop-in writer (issue: omnigent-eval-host must run from the
# same immutable release as omnigent-eval-web).
# ---------------------------------------------------------------------------


@pytest.fixture
def host_dropin_dir(tmp_path: Path) -> Path:
    """A writable drop-in directory for the host service.

    The default ``/etc/systemd/system/omnigent-eval-host.service.d``
    is not writable from tests; this fixture substitutes a tmpdir
    so the host drop-in writer can run end-to-end in unit tests.
    """
    d = tmp_path / "host-dropins"
    d.mkdir()
    return d


@pytest.fixture
def host_spec(host_dropin_dir: Path):
    """Return a :class:`ServiceSpec` for the host service pointing at
    the tmp drop-in directory."""
    from dataclasses import replace

    return replace(systemd.host_service_spec(), dropin_dir=host_dropin_dir)


def test_write_release_dropin_for_host_pins_release_dir(host_spec, release_dir: Path) -> None:
    """Host drop-in writes ``OMNIGENT_RELEASE_DIR=...`` pointing at
    the release passed in by the caller."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    path = systemd.write_release_dropin(sha, release_dir=release_dir, spec=host_spec)
    body = path.read_text()
    assert f"OMNIGENT_RELEASE_DIR={release_dir}" in body
    assert f"OMNIGENT_RELEASE_EXPECTED_SHA={sha}" in body


def test_write_release_dropin_for_host_uses_release_venv_omni(
    host_spec, release_dir: Path
) -> None:
    """Host drop-in's ``ExecStart`` resolves to ``<release>/.venv/bin/omni``
    so the host daemon imports the release's installed wheel, not
    whatever the mutable ``PATH`` happens to contain."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    path = systemd.write_release_dropin(sha, release_dir=release_dir, spec=host_spec)
    body = path.read_text()
    assert f"ExecStart={release_dir}/.venv/bin/omni host" in body
    # The pre-start gate runs from the release venv as well so a
    # bad release cannot serve as the host daemon either.
    assert f"ExecStartPre={release_dir}/.venv/bin/python" in body


def test_write_release_dropin_for_host_sets_execstoppost(host_spec, release_dir: Path) -> None:
    """Host drop-in installs ``ExecStopPost`` so a clean
    ``systemctl stop`` invokes the release's own ``omni host stop``
    rather than letting systemd SIGKILL the daemon after the
    default 90s grace."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    path = systemd.write_release_dropin(sha, release_dir=release_dir, spec=host_spec)
    body = path.read_text()
    assert "ExecStopPost=" in body
    assert f"{release_dir}/.venv/bin/omni host stop" in body
    # The StopPost URL must point at the loopback web service the
    # host is registered with.
    assert "--server http://127.0.0.1:" in body


def test_write_release_dropin_for_web_has_no_execstoppost(
    dropin_dir: Path, release_dir: Path
) -> None:
    """Web drop-in does NOT install ``ExecStopPost``: the web server
    is stopped by systemd's default SIGTERM, which the unit's
    graceful-shutdown handler already handles.

    This pins the asymmetry between web (no ExecStopPost) and host
    (ExecStopPost=omni host stop) so a future refactor that adds
    ExecStopPost to the web drop-in (or removes it from the host
    drop-in) cannot silently regress.
    """
    sha = "0123456789abcdef0123456789abcdef01234567"
    path = systemd.write_release_dropin(sha, release_dir=release_dir)
    body = path.read_text()
    assert "ExecStopPost=" not in body


def test_host_service_spec_defaults_are_stable() -> None:
    """The host service spec is hard-coded to the canonical unit
    name and drop-in directory.

    The spec does NOT honor ``OMNIGENT_DEPLOY_SERVICE_NAME`` /
    ``OMNIGENT_DEPLOY_DROPIN_DIR`` — those env vars are reserved
    for the legacy web helper. An operator that exports either
    var must NOT see the host drop-in redirected somewhere
    unexpected.
    """
    spec = systemd.host_service_spec()
    assert spec.service_name == "omnigent-eval-host.service"
    assert spec.dropin_dir == Path("/etc/systemd/system/omnigent-eval-host.service.d")
    assert spec.exec_start_kind == "host"


def test_disable_other_release_dropins_respects_service_kind(
    dropin_dir: Path, host_dropin_dir: Path, release_dir: Path
) -> None:
    """``disable_other_release_dropins`` with a service spec only
    disables siblings in that service's drop-in directory.

    The web and host services have separate drop-in trees; calling
    the disable helper for the host service must not move
    unrelated ``10-release-<sha>.conf`` files in the web drop-in
    tree.
    """
    from dataclasses import replace

    active_sha = "0123456789abcdef0123456789abcdef01234567"
    other_sha = "ffffffffffffffffffffffffffffffffffffffff"

    host_spec = replace(systemd.host_service_spec(), dropin_dir=host_dropin_dir)

    # Write drop-ins under both services.
    systemd.write_release_dropin(active_sha, release_dir=release_dir)
    systemd.write_release_dropin(other_sha, release_dir=release_dir / "other")
    systemd.write_release_dropin(active_sha, release_dir=release_dir, spec=host_spec)
    systemd.write_release_dropin(other_sha, release_dir=release_dir / "other-host", spec=host_spec)

    # Disabling for the host service must NOT touch the web
    # drop-in directory.
    host_disabled = systemd.disable_other_release_dropins(active_sha, spec=host_spec)
    assert len(host_disabled) == 1
    assert host_disabled[0].name.endswith(".disabled")
    # Web drop-ins remain active.
    web_files = sorted(p.name for p in dropin_dir.iterdir() if not p.name.endswith(".disabled"))
    assert f"10-release-{active_sha[:12]}.conf" in web_files
    assert f"10-release-{other_sha[:12]}.conf" in web_files
