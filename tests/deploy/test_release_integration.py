"""End-to-end test of the release layout, using a small fake repo so we
can exercise the orchestration logic without touching the real
deployments tree or systemd.

The real live migration uses git, uv, npm — those are not usable
inside the test environment without bringing real operations in. This
test exercises the layout, provenance, and gate modules at the unit-
of-work level by composing them against fake releases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.deploy.ops import systemd
from omnigent.deploy.preflight import expected_web_ui_dir
from omnigent.deploy.supervisor.gate import run_gate
from omnigent.deploy.supervisor.manifest import (
    ReleaseManifest,
    write_manifest,
)


@pytest.fixture
def deploy_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIGENT_DEPLOY_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_release(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (release / ".venv" / "pyvenv.cfg").write_text("home = /tmp\n")
    (site_packages / "omnigent").mkdir()
    (site_packages / "omnigent" / "__init__.py").write_text("")
    (site_packages / "omnigent" / "server").mkdir()
    (site_packages / "omnigent" / "server" / "__init__.py").write_text("")
    (site_packages / "omnigent" / "server" / "app.py").write_text("")
    bundle = expected_web_ui_dir(release)
    bundle.mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html>")
    (bundle / "version.json").write_text('{"build": "test"}')
    (bundle / "manifest.webmanifest").write_text("{}")
    return release


@pytest.fixture
def stub_provenance(monkeypatch: pytest.MonkeyPatch, fake_release: Path) -> None:
    """Make ``check_runtime_provenance`` succeed on the fake release."""
    import omnigent.deploy.supervisor.provenance as prov

    site_packages = fake_release / ".venv" / "lib" / "python3.12" / "site-packages"

    monkeypatch.setattr(
        prov, "_resolve_executable", lambda: fake_release / ".venv" / "bin" / "python"
    )
    monkeypatch.setattr(prov, "_resolve_prefix", lambda: fake_release)

    def fake_module(name: str, *, attr: str | None = None) -> Path:
        if name == "omnigent":
            return site_packages / "omnigent" / "__init__.py"
        if name == "omnigent.server":
            return site_packages / "omnigent" / "server" / "app.py"
        raise AssertionError(name)

    monkeypatch.setattr(prov, "_resolve_module", fake_module)


def test_layout_releases_manifests_failed_layout(deploy_root: Path) -> None:
    """``deploy_root()`` creates the canonical subdirectory layout when accessed."""
    # Call the helper to lazy-create the layout; the function returns the
    # root and ``releases()`` / ``manifests()`` / ``failed()`` each
    # ensure their subdir exists on access.
    from omnigent.deploy.ops import layout as layout_mod

    layout_mod.deploy_root()
    layout_mod.releases_dir()
    layout_mod.manifests_dir()
    layout_mod.failed_dir()
    assert (deploy_root / "releases").is_dir()
    assert (deploy_root / "manifests").is_dir()
    assert (deploy_root / "failed").is_dir()


def test_release_with_full_provenance_passes_gate(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_RELEASE_EXPECTED_SHA", raising=False)
    info = run_gate(fake_release)
    assert info["skip_web_ui"] == "0"


def test_release_without_manifest_when_no_expected_sha(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release without a manifest is acceptable when no SHA pinning is requested.

    Useful for ad-hoc checkout tests where the promotion script has not
    run yet but an operator wants to smoke-test the service.
    """
    monkeypatch.delenv("OMNIGENT_RELEASE_EXPECTED_SHA", raising=False)
    info = run_gate(fake_release)
    assert info["manifest_sha"] == ""


def test_release_with_manifest_matches_expected_sha(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "OMNIGENT_RELEASE_EXPECTED_SHA",
        "0123456789abcdef0123456789abcdef01234567",
    )
    (fake_release / "uv.lock").write_text("# uv lock\n")
    manifest = ReleaseManifest(
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir=str(fake_release),
        python_executable=str(fake_release / ".venv" / "bin" / "python"),
        python_version="3.12.13",
        omnigent_module_path=str(
            fake_release
            / ".venv"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "omnigent"
            / "__init__.py"
        ),
        omnigent_server_app_path=str(
            fake_release
            / ".venv"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "omnigent"
            / "server"
            / "app.py"
        ),
        lockfile_hashes={"uv.lock": _sha256(fake_release / "uv.lock")},
    )
    write_manifest(fake_release, manifest)
    info = run_gate(fake_release)
    assert info["manifest_sha"] == "0123456789abcdef0123456789abcdef01234567"


def test_drop_in_reflects_release_choice(deploy_root: Path, tmp_path: Path) -> None:
    """A second release gets a different drop-in; previous cleanup keeps
    only the active one.

    Mirrors the script flow: write active drop-in, write older one,
    disable the older one. We invoke the underlying python functions
    directly because the script does ``sudo`` for the drop-in dir.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("OMNIGENT_DEPLOY_DROPIN_DIR", str(deploy_root / "dropins"))
        (deploy_root / "dropins").mkdir()
        sha_active = "0123456789abcdef0123456789abcdef01234567"
        sha_old = "ffffffffffffffffffffffffffffffffffffffff"
        rel_active = tmp_path / "active"
        rel_old = tmp_path / "old"
        rel_active.mkdir()
        rel_old.mkdir()
        systemd.write_release_dropin(sha_active, release_dir=rel_active)
        systemd.write_release_dropin(sha_old, release_dir=rel_old)
        disabled = systemd.disable_other_release_dropins(sha_active)
        assert (deploy_root / "dropins" / f"10-release-{sha_old[:12]}.conf.disabled").is_file()
        # Active drop-in survives.
        assert (deploy_root / "dropins" / f"10-release-{sha_active[:12]}.conf").is_file()
        assert any(p.name.endswith(".disabled") for p in disabled)
    finally:
        monkeypatch.undo()


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
