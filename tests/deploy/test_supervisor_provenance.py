"""Tests for the runtime provenance gate.

The gate is the load-bearing check that prevents a stale or edited venv
from quietly shadowing the configured release: if the venv imports
``omnigent`` from somewhere other than the release directory, the
service must refuse to start. These tests pin that behavior so a
future refactor cannot silently weaken it.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

from omnigent.deploy.supervisor import provenance as prov


@pytest.fixture
def fake_release(tmp_path: Path) -> Path:
    """Build a release directory whose ``.venv/bin/python`` reports
    ``__file__`` paths inside ``tmp_path``.

    The provenance check inspects the *running* interpreter, so the
    tests override :func:`prov._resolve_module` and :func:`prov._resolve_prefix`
    with deterministic stubs that point at controlled paths. The
    fixture's job is to provide a ``.venv/pyvenv.cfg`` AND a
    populated ``site-packages`` directory so the new strict
    site-packages check passes for the *happy-path* tests.
    """
    release = tmp_path / "release"
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (release / ".venv" / "pyvenv.cfg").write_text("home = /tmp\n")
    # Module files live under site-packages (the only legitimate location).
    (site_packages / "omnigent").mkdir()
    (site_packages / "omnigent" / "__init__.py").write_text("")
    (site_packages / "omnigent" / "server").mkdir()
    (site_packages / "omnigent" / "server" / "__init__.py").write_text("")
    (site_packages / "omnigent" / "server" / "app.py").write_text("")
    return release


def test_check_runtime_provenance_passes_when_all_paths_inside_release(
    fake_release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All paths (prefix, omnigent, server.app) live in the release's site-packages."""

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
        raise AssertionError(f"unexpected module probe: {name}")

    monkeypatch.setattr(prov, "_resolve_executable", fake_exe)
    monkeypatch.setattr(prov, "_resolve_prefix", fake_prefix)
    monkeypatch.setattr(prov, "_resolve_module", fake_module)

    info = prov.check_runtime_provenance(fake_release)
    assert info["release"].endswith("release")
    assert info["sys_prefix"] == str(fake_release.resolve())
    assert info["site_packages"] == str(site_packages.resolve())
    assert info["omnigent_module"].endswith("site-packages/omnigent/__init__.py")
    assert info["omnigent_server_app"].endswith("server/app.py")


def test_check_runtime_provenance_releases_outside_prefix(
    fake_release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefix that lives outside the release triggers the gate."""

    other_prefix = fake_release.parent / "other-checkout"
    other_prefix.mkdir(parents=True)

    def fake_prefix() -> Path:
        return other_prefix

    monkeypatch.setattr(prov, "_resolve_executable", lambda: fake_release / ".venv" / "bin" / "python")
    monkeypatch.setattr(prov, "_resolve_prefix", fake_prefix)
    monkeypatch.setattr(prov, "_resolve_module", lambda *a, **k: fake_release / "x")

    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(fake_release)
    assert "sys.prefix" in str(exc.value)
    assert exc.value.diverging == other_prefix


def test_check_runtime_provenance_rejects_editable_install(
    fake_release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``omnigent`` resolving from the main checkout (not the release's
    site-packages) is rejected.

    The previous version of this test pinned ``"editable install"`` in
    the error message; the strengthened check now reports the actual
    diverging path so the operator can see whether it was the bare
    release source root, the repo checkout, or some other checkout.
    """
    site_packages = fake_release / ".venv" / "lib" / "python3.12" / "site-packages"
    main_omnigent = fake_release.parent / "main-checkout" / "omnigent" / "__init__.py"
    main_omnigent.parent.mkdir(parents=True)
    main_omnigent.write_text("")

    def fake_prefix() -> Path:
        return fake_release

    def fake_module(name: str, *, attr: str | None = None) -> Path:
        if name == "omnigent":
            return main_omnigent
        if name == "omnigent.server":
            return site_packages / "omnigent" / "server" / "app.py"
        raise AssertionError(f"unexpected module probe: {name}")

    monkeypatch.setattr(prov, "_resolve_executable", lambda: fake_release / ".venv" / "bin" / "python")
    monkeypatch.setattr(prov, "_resolve_prefix", fake_prefix)
    monkeypatch.setattr(prov, "_resolve_module", fake_module)

    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(fake_release)
    assert "site-packages" in str(exc.value)
    assert exc.value.diverging == main_omnigent


def test_check_runtime_provenance_requires_pyvenv_cfg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release with a symlinked venv (no ``pyvenv.cfg`` in the release) is rejected."""

    release = tmp_path / "release"
    (release / ".venv").mkdir(parents=True)
    # no pyvenv.cfg

    monkeypatch.setattr(prov, "_resolve_executable", lambda: release / ".venv" / "bin" / "python")
    monkeypatch.setattr(prov, "_resolve_prefix", lambda: release)
    monkeypatch.setattr(prov, "_resolve_module", lambda *a, **k: release / "x")

    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(release)
    assert "pyvenv.cfg" in str(exc.value)


def test_check_runtime_provenance_rejects_missing_release(tmp_path: Path) -> None:
    """A non-existent release directory raises with a clear message."""
    missing = tmp_path / "does-not-exist"
    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(missing)
    assert "does not exist" in str(exc.value)


def test_check_runtime_provenance_uses_real_paths_for_smoke() -> None:
    """A real-environment canary that exercises the new strict path.

    When the test runs from this repo's editable ``.venv``, the
    stricter check MUST now refuse: an editable install is no longer
    acceptable provenance. The previous version of this test expected
    a pass under those conditions (the original bug the brief calls
    out); this version pins the corrected behavior so a future
    refactor that re-loosens the check breaks the test here.
    """
    import sys

    release = Path(__file__).resolve().parents[2]
    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(release)
    # The check has multiple failure modes depending on which Python
    # ran the test session (editable finder, foreign .pth, foreign
    # ``omnigent`` resolution); accept any of them so the test stays
    # stable across Python 3.12/3.13, uv-managed and homebrew base
    # interpreters, etc.
    msg = str(exc.value).lower()
    assert any(
        marker in msg
        for marker in (
            "editable",
            "site-packages",
            "sys.prefix",
            "pyvenv.cfg",
        )
    ), f"provenance refused the editable-install checkout for an unexpected reason: {exc.value}"
