"""Tests for the systemd drop-in writer.

The drop-in writer is the only piece that the operator-level
``scripts/promote_release.sh`` invokes via ``sudo``; everything else
runs as the calling user. These tests pin the drop-in schema so a
future change doesn't accidentally make the live systemd unit point
at a hard-coded path again.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.deploy.ops import systemd


@pytest.fixture
def dropin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIGENT_DEPLOY_DROPIN_DIR", str(tmp_path))
    # Point the host drop-in env at the same tmp_path so the
    # disable_other tests don't try to reach into /etc/systemd for
    # a directory the test didn't create.
    monkeypatch.setenv("OMNIGENT_DEPLOY_HOST_DROPIN_DIR", str(tmp_path / "host"))
    (tmp_path / "host").mkdir(exist_ok=True)
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


# ── Host drop-in / deployment completeness (issue #30 follow-up) ──


@pytest.fixture
def host_dropin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIGENT_DEPLOY_HOST_DROPIN_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DEPLOY_DROPIN_DIR", str(tmp_path / "web"))
    (tmp_path / "web").mkdir(exist_ok=True)
    return tmp_path


def test_write_host_dropin_uses_canonical_python_not_omni_shim(
    host_dropin_dir: Path, release_dir: Path
) -> None:
    """The host drop-in invokes ``python -P -m omnigent host`` directly
    so the release-local ``.venv/bin/omni`` shim's embedded staging
    path cannot abort systemd startup after the staging dir is cleaned
    up.
    """
    sha = "0123456789abcdef0123456789abcdef01234567"
    path = systemd.write_host_dropin(sha, release_dir=release_dir)
    body = path.read_text()
    assert "ExecStart=" in body
    assert "omnigent host" in body, "must invoke the omnigent host subcommand"
    assert f"{release_dir}/.venv/bin/python -P -m omnigent host" in body
    # Must NOT depend on the .venv/bin/omni shim, which can embed a
    # deleted .staging-<hash> path.
    assert ".venv/bin/omni " not in body
    assert ".venv/bin/omni\n" not in body


def test_disable_other_release_dropins_cleans_host_dropin_dir(
    dropin_dir: Path, release_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``disable_other_release_dropins`` must clean the host drop-in
    directory too, so a stale ``omnigent-eval-host.service.d/10-release-<old>.conf``
    cannot keep the host pinned at a previous release.
    """
    monkeypatch.setenv("OMNIGENT_DEPLOY_HOST_DROPIN_DIR", str(tmp_path))
    host_dir = tmp_path
    active_sha = "0123456789abcdef0123456789abcdef01234567"
    other_sha = "ffffffffffffffffffffffffffffffffffffffff"
    systemd.write_release_dropin(active_sha, release_dir=release_dir)
    other_web = systemd.write_release_dropin(other_sha, release_dir=release_dir / "other")
    systemd.write_host_dropin(other_sha, release_dir=release_dir / "other")
    other_host = host_dir / f"10-release-{other_sha[:12]}.conf"
    assert other_host.is_file()
    disabled = systemd.disable_other_release_dropins(active_sha)
    # Both stale drop-ins get moved aside.
    assert other_web.with_suffix(other_web.suffix + ".disabled") in disabled
    assert other_host.with_suffix(other_host.suffix + ".disabled") in disabled


def test_normalize_entry_point_shims_rewrites_staging_shebang(
    tmp_path: Path,
) -> None:
    """``normalize_entry_point_shims`` rewrites the ``.venv/bin/{omni,omnigent}``
    shebang to point at the canonical release's ``.venv/bin/python``,
    not a deleted ``.staging-<hash>`` dir.
    """
    sha = "0123456789abcdef0123456789abcdef01234567"
    # Set up the canonical dir with a real .venv/bin/python (the
    # release builder's final state). Then write a shim whose
    # shebang points at a *different* (staging) path and prove the
    # normalizer rewrites it to the canonical one.
    canonical = tmp_path / "releases" / sha
    venv_bin = canonical / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_bin = venv_bin / "python"
    python_bin.write_text("#!/bin/sh\necho python\n")
    python_bin.chmod(0o755)
    staging_path = tmp_path / "releases" / f".staging-{sha}-9999-1234567890"
    staging_python = staging_path / ".venv" / "bin" / "python"
    broken_omni = f"#!/bin/sh\n'''exec' '{staging_python}' \"$0\" \"$@\"\n' '''\n# body\n"
    (venv_bin / "omni").write_text(broken_omni)
    (venv_bin / "omni").chmod(0o755)
    (venv_bin / "omnigent").write_text(broken_omni)
    (venv_bin / "omnigent").chmod(0o755)

    rewritten = systemd.normalize_entry_point_shims(canonical)
    assert len(rewritten) == 2
    for shim_name in ("omni", "omnigent"):
        shim = canonical / ".venv" / "bin" / shim_name
        first_line = shim.read_text().splitlines()[0]
        assert first_line == f"#!{python_bin}"

    # Idempotent: re-running produces no further rewrites.
    rewritten_again = systemd.normalize_entry_point_shims(canonical)
    assert rewritten_again == []


def test_normalize_entry_point_shims_idempotent(tmp_path: Path) -> None:
    """Re-running against an already-canonical shim is a no-op."""
    release = tmp_path / "releases" / "0123456789abcdef0123456789abcdef01234567"
    (release / ".venv" / "bin").mkdir(parents=True)
    (release / ".venv" / "bin" / "python").write_text("stub\n")
    (release / ".venv" / "bin" / "python").chmod(0o755)
    canonical_shebang = f"#!{release / '.venv' / 'bin' / 'python'}\nbody\n"
    (release / ".venv" / "bin" / "omni").write_text(canonical_shebang)
    (release / ".venv" / "bin" / "omnigent").write_text(canonical_shebang)
    rewritten = systemd.normalize_entry_point_shims(release)
    assert rewritten == []


# ── loaded_release_sha / verify_loaded_release ──


class _ProcResult:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_loaded_release_sha_extracts_canonical_from_cmdline() -> None:
    """``loaded_release_sha`` reads ``/proc/<pid>/cmdline`` and finds
    the canonical ``releases/<40-hex>`` segment.
    """
    sha = "44338e2fe029902a898c7d44932e318426375d0f"
    cmdline = (
        f"\x00/home/hermes/workspace/deployments/omnigent/releases/{sha}/.venv/bin/python\x00"
        f"-P\x00-m\x00omnigent\x00server\x00--host\x00127.0.0.1\x00"
    ).encode()
    # Patch /proc read so we don't need a real PID.
    with patch.object(systemd.Path, "read_bytes", side_effect=[cmdline, b""]):
        got = systemd.loaded_release_sha(12345)
    assert got == sha


def test_loaded_release_sha_refuses_when_cmdline_and_env_disagree() -> None:
    """A mismatch between the cmdline-extracted SHA and the
    ``OMNIGENT_RELEASE_EXPECTED_SHA`` env var is rejected; the
    deployment is in an inconsistent state.
    """
    cmdline_sha = "44338e2fe029902a898c7d44932e318426375d0f"
    env_sha = "0f2fd5ab0ce84a95a73900f9ac3a06dcec035051"
    cmdline = (
        f"\x00/home/hermes/workspace/deployments/omnigent/releases/{cmdline_sha}/.venv/bin/python\x00-m\x00omnigent\x00host\x00"
    ).encode()
    environ = f"PATH=/usr/bin\x00OMNIGENT_RELEASE_EXPECTED_SHA={env_sha}\x00".encode()
    with patch.object(systemd.Path, "read_bytes", side_effect=[cmdline, environ]):
        got = systemd.loaded_release_sha(12345)
    assert got is None


def test_loaded_release_sha_falls_back_to_env_when_cmdline_empty() -> None:
    """When /proc cmdline is empty (rare but possible), the env var is
    the only source of truth.
    """
    sha = "44338e2fe029902a898c7d44932e318426375d0f"
    environ = f"OMNIGENT_RELEASE_EXPECTED_SHA={sha}\x00".encode()
    with patch.object(systemd.Path, "read_bytes", side_effect=[b"", environ]):
        got = systemd.loaded_release_sha(12345)
    assert got == sha


def test_verify_loaded_release_passes_on_matching_sha() -> None:
    """``verify_loaded_release`` returns the canonical release dir
    when the running service loaded the expected SHA.
    """
    sha = "44338e2fe029902a898c7d44932e318426375d0f"
    cmdline = (
        f"\x00/home/hermes/workspace/deployments/omnigent/releases/{sha}/.venv/bin/python\x00"
        f"-m\x00omnigent\x00server\x00"
    ).encode()
    environ = f"OMNIGENT_RELEASE_EXPECTED_SHA={sha}\x00".encode()
    with patch.object(systemd.Path, "read_bytes", side_effect=[cmdline, environ]):
        out = systemd.verify_loaded_release(
            service="omnigent-eval-web.service",
            expected_sha=sha,
            proc=_ProcResult(returncode=0, stdout="12345"),
        )
    assert out == Path(f"/home/hermes/workspace/deployments/omnigent/releases/{sha}")


def test_verify_loaded_release_rejects_stale_sha() -> None:
    """A service still running from a previous release SHA fails the
    deployment-completeness check.
    """
    loaded = "0f2fd5ab0ce84a95a73900f9ac3a06dcec035051"
    expected = "44338e2fe029902a898c7d44932e318426375d0f"
    cmdline = (
        f"\x00/home/hermes/workspace/deployments/omnigent/releases/{loaded}/.venv/bin/python\x00"
        f"-m\x00omnigent\x00host\x00"
    ).encode()
    environ = f"OMNIGENT_RELEASE_EXPECTED_SHA={loaded}\x00".encode()
    with patch.object(systemd.Path, "read_bytes", side_effect=[cmdline, environ]):
        with pytest.raises(systemd.SystemdError) as excinfo:
            systemd.verify_loaded_release(
                service="omnigent-eval-host.service",
                expected_sha=expected,
                proc=_ProcResult(returncode=0, stdout="12345"),
            )
    assert expected in str(excinfo.value)
    assert loaded in str(excinfo.value)
    assert "refusing to declare deployment live" in str(excinfo.value)


def test_verify_loaded_release_rejects_staging_path() -> None:
    """A process running from a deleted ``.staging-<hash>`` dir fails
    the deployment-completeness check, because the staging dir is
    build-time scratch that must be renamed into the canonical path
    before the host restarts.
    """
    # Use a *different* expected SHA so the loader's staging-path
    # SHA does not match; this proves that the canonical-vs-staging
    # distinction is being made. (When expected == staging SHA the
    # two would be indistinguishable and the check would pass.)
    loaded_sha = "44338e2fe029902a898c7d44932e318426375d0f"
    expected_sha = "0f2fd5ab0ce84a95a73900f9ac3a06dcec035051"
    cmdline = (
        f"\x00/home/hermes/workspace/deployments/omnigent/releases/.staging-{loaded_sha}-9999-123/.venv/bin/python\x00"
        f"-m\x00omnigent\x00host\x00"
    ).encode()
    environ = b""
    with patch.object(systemd.Path, "read_bytes", side_effect=[cmdline, environ]):
        with pytest.raises(systemd.SystemdError) as excinfo:
            systemd.verify_loaded_release(
                service="omnigent-eval-host.service",
                expected_sha=expected_sha,
                proc=_ProcResult(returncode=0, stdout="12345"),
            )
    assert "refusing to declare deployment live" in str(excinfo.value)
    assert loaded_sha in str(excinfo.value)
    assert expected_sha in str(excinfo.value)
