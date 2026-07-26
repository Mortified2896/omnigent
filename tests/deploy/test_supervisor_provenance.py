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
    fixture's job is to provide a ``.venv/pyvenv.cfg`` so the
    structural side of the check passes.
    """
    release = tmp_path / "release"
    (release / ".venv").mkdir(parents=True)
    (release / ".venv" / "pyvenv.cfg").write_text("home = /tmp\n")
    (release / "omnigent").mkdir(parents=True)
    (release / "omnigent" / "__init__.py").write_text("")
    (release / "omnigent" / "server").mkdir(parents=True)
    (release / "omnigent" / "server" / "__init__.py").write_text("")
    (release / "omnigent" / "server" / "app.py").write_text("")
    return release


def test_check_runtime_provenance_passes_when_all_paths_inside_release(
    fake_release: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All paths (prefix, omnigent, server.app) live in the release."""

    def fake_exe() -> Path:
        return fake_release / ".venv" / "bin" / "python"

    def fake_prefix() -> Path:
        return fake_release

    def fake_module(name: str, *, attr: str | None = None) -> Path:
        if name == "omnigent":
            return fake_release / "omnigent" / "__init__.py"
        if name == "omnigent.server":
            return fake_release / "omnigent" / "server" / "app.py"
        raise AssertionError(f"unexpected module probe: {name}")

    monkeypatch.setattr(prov, "_resolve_executable", fake_exe)
    monkeypatch.setattr(prov, "_resolve_prefix", fake_prefix)
    monkeypatch.setattr(prov, "_resolve_module", fake_module)

    info = prov.check_runtime_provenance(fake_release)
    assert info["release"].endswith("release")
    assert info["sys_prefix"] == str(fake_release.resolve())
    assert info["omnigent_module"].endswith("omnigent/__init__.py")
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
    """``omnigent`` resolving from the main checkout (not the release) is rejected."""

    main_omnigent = fake_release.parent / "main-checkout" / "omnigent" / "__init__.py"
    main_omnigent.parent.mkdir(parents=True)
    main_omnigent.write_text("")

    def fake_prefix() -> Path:
        return fake_release

    def fake_module(name: str, *, attr: str | None = None) -> Path:
        if name == "omnigent":
            return main_omnigent
        if name == "omnigent.server":
            return fake_release / "omnigent" / "server" / "app.py"
        raise AssertionError(f"unexpected module probe: {name}")

    monkeypatch.setattr(prov, "_resolve_executable", lambda: fake_release / ".venv" / "bin" / "python")
    monkeypatch.setattr(prov, "_resolve_prefix", fake_prefix)
    monkeypatch.setattr(prov, "_resolve_module", fake_module)

    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(fake_release)
    assert "editable install" in str(exc.value)
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
    """A real-environment canary.

    When the test is run from this repo's existing ``.venv`` (which
    uses an editable install pointing at this checkout), the gate
    fails because ``sys.prefix`` is the current ``.venv`` (which is
    inside the repo checkout) but the omnigent module resolves to the
    same repo checkout — so the check passes.

    Useful as a live wiring test; future changes that re-target
    ``sys.prefix`` or the module resolver will trip this test before
    they reach production.
    """
    import sys

    release = Path(__file__).resolve().parents[2]
    info = prov.check_runtime_provenance(release)
    # The real test session always runs from a venv inside the repo,
    # so the prefix lives inside the repo and ``omnigent`` resolves
    # there too.
    assert info["release"].endswith("omnigent-eval")
    assert info["sys_prefix"].startswith(str(release))
    assert info["omnigent_module"].endswith("omnigent/__init__.py")
