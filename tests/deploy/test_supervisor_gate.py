"""Tests for the systemd ``ExecStartPre`` supervisor gate.

The gate is what refuses to start omnigent-eval-web.service when the
release is misconfigured. It composes three independent checks:

* the runtime provenance (``omnigent`` loads from inside the release),
* the manifest SHA match,
* the web UI bundle preflight (unless explicit API-only).

The API-only escape hatch is the only legitimate way to bring the
service up without the bundle: setting ``OMNIGENT_SKIP_WEB_UI=true``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.deploy.preflight import expected_web_ui_dir
from omnigent.deploy.supervisor import manifest as manifest_mod
from omnigent.deploy.supervisor.gate import GateError, run_gate


@pytest.fixture
def fake_release(tmp_path: Path) -> Path:
    """Build a release with the structural shape the gate expects.

    The new strict provenance check requires the module files to live
    inside ``.venv/lib/pythonX.Y/site-packages``, not at the bare
    ``<release>/omnigent`` source tree. The fixture creates both so
    that happy-path tests can route the module resolver into
    site-packages, while bundle / manifest tests still operate on
    the bare source tree.
    """
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
    (bundle / "version.json").write_text('{"build": "abc"}')
    (bundle / "manifest.webmanifest").write_text("{}")
    return release


@pytest.fixture
def stub_provenance(monkeypatch: pytest.MonkeyPatch, fake_release: Path) -> None:
    """Make the provenance check pass on the fake release."""

    site_packages = fake_release / ".venv" / "lib" / "python3.12" / "site-packages"

    def fake_exe() -> Path:
        return fake_release / ".venv" / "bin" / "python"

    def fake_prefix() -> Path:
        return fake_release

    def fake_module(name: str, *, attr: str | None = None) -> Path:
        if name == "omnigent":
            return site_packages / "omnigent" / "__init__.py"
        if name == "omnigent.server":
            return site_packages / "omnigent" / "server" / "app.py"
        raise AssertionError(name)

    import omnigent.deploy.supervisor.provenance as prov

    monkeypatch.setattr(prov, "_resolve_executable", fake_exe)
    monkeypatch.setattr(prov, "_resolve_prefix", fake_prefix)
    monkeypatch.setattr(prov, "_resolve_module", fake_module)


def test_run_gate_passes_when_all_checks_pass(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean release passes the gate."""
    # No manifest SHA env var → manifest check is skipped.
    monkeypatch.delenv("OMNIGENT_RELEASE_EXPECTED_SHA", raising=False)
    # The CI workflow sets OMNIGENT_SKIP_WEB_UI=true so the bundle check is
    # not required during the pytest run; the gate would otherwise force
    # skip_web_ui="1" and the strict assertion below would see it.
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    info = run_gate(fake_release)
    assert info["skip_web_ui"] == "0"
    assert info["release"].endswith("release")
    assert info["omnigent_module"].endswith("omnigent/__init__.py")


def test_run_gate_rejects_missing_bundle_in_normal_mode(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release without a built bundle fails the gate with a runbook."""
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    monkeypatch.delenv("OMNIGENT_RELEASE_EXPECTED_SHA", raising=False)
    bundle = expected_web_ui_dir(fake_release)
    for f in bundle.iterdir():
        f.unlink()
    bundle.rmdir()

    with pytest.raises(GateError) as exc:
        run_gate(fake_release)
    assert "web UI bundle" in str(exc.value)


def test_run_gate_allows_explicit_api_only(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting ``OMNIGENT_SKIP_WEB_UI=true`` skips the bundle check."""
    monkeypatch.setenv("OMNIGENT_SKIP_WEB_UI", "true")
    monkeypatch.delenv("OMNIGENT_RELEASE_EXPECTED_SHA", raising=False)
    bundle = expected_web_ui_dir(fake_release)
    for f in bundle.iterdir():
        f.unlink()
    bundle.rmdir()

    info = run_gate(fake_release, skip_web_ui=True)
    assert info["skip_web_ui"] == "1"


def test_run_gate_verifies_manifest_sha_when_provided(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release with a manifest whose SHA matches the expected SHA passes."""
    expected_sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("OMNIGENT_RELEASE_EXPECTED_SHA", expected_sha)
    # See test_run_gate_passes_when_all_checks_pass for why this delenv is
    # required even though the manifest case does not exercise the bundle
    # check directly.
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    site_packages = fake_release / ".venv" / "lib" / "python3.12" / "site-packages"
    manifest = manifest_mod.ReleaseManifest(
        commit_sha=expected_sha,
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir=str(fake_release),
        python_executable=str(fake_release / ".venv" / "bin" / "python"),
        python_version="3.12.13",
        omnigent_module_path=str(site_packages / "omnigent" / "__init__.py"),
        omnigent_server_app_path=str(site_packages / "omnigent" / "server" / "app.py"),
    )
    path = manifest_mod.write_manifest(fake_release, manifest)

    info = run_gate(fake_release)
    assert info["manifest_sha"] == expected_sha
    assert info["skip_web_ui"] == "0"


def test_run_gate_rejects_manifest_sha_mismatch(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release with a manifest pointing at a different SHA is refused."""
    monkeypatch.setenv("OMNIGENT_RELEASE_EXPECTED_SHA", "0123456789abcdef0123456789abcdef01234567")
    site_packages = fake_release / ".venv" / "lib" / "python3.12" / "site-packages"
    manifest = manifest_mod.ReleaseManifest(
        commit_sha="ffffffffffffffffffffffffffffffffffffffff",
        built_at="2026-07-26T00:00:00Z",
        repository="Mortified2896/omnigent",
        release_dir=str(fake_release),
        python_executable=str(fake_release / ".venv" / "bin" / "python"),
        python_version="3.12.13",
        omnigent_module_path=str(site_packages / "omnigent" / "__init__.py"),
        omnigent_server_app_path=str(site_packages / "omnigent" / "server" / "app.py"),
    )
    manifest_mod.write_manifest(fake_release, manifest)
    with pytest.raises(GateError) as exc:
        run_gate(fake_release)
    assert "manifest" in str(exc.value).lower() and "mismatch" in str(exc.value).lower()


def test_run_gate_rejects_missing_manifest(
    fake_release: Path, stub_provenance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release whose manifest is missing is rejected when an expected SHA is set."""
    monkeypatch.setenv("OMNIGENT_RELEASE_EXPECTED_SHA", "0123456789abcdef0123456789abcdef01234567")
    with pytest.raises(GateError) as exc:
        run_gate(fake_release)
    assert "manifest" in str(exc.value).lower()


def test_run_gate_releases_provenance_failure_first(
    fake_release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance is checked before the bundle; both would fail, only the
    first surfaces.

    The point of ordering is to surface the most likely cause of the
    failure so the operator doesn't chase the bundle rebuild when the
    venv is the actual problem.
    """
    import omnigent.deploy.supervisor.provenance as prov

    other_prefix = fake_release.parent / "elsewhere"
    other_prefix.mkdir()

    monkeypatch.setattr(
        prov,
        "_resolve_executable",
        lambda: fake_release / ".venv" / "bin" / "python",
    )
    monkeypatch.setattr(prov, "_resolve_prefix", lambda: other_prefix)
    monkeypatch.setattr(prov, "_resolve_module", lambda *a, **k: fake_release / "x")
    monkeypatch.delenv("OMNIGENT_RELEASE_EXPECTED_SHA", raising=False)
    with pytest.raises(GateError) as exc:
        run_gate(fake_release)
    assert "provenance" in str(exc.value).lower() or "interpreter" in str(exc.value).lower()
