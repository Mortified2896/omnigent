"""Behavioral tests for the strengthened provenance check.

The previous provenance module accepted "omnigent resolves anywhere
inside the release" — which is too loose because running the probe
from the repository checkout makes Python import
``<release>/omnigent/__init__.py`` (the bare source tree) instead of
the installed wheel. These tests pin the new strict contract:

* a proper non-editable release installation passes;
* running the probe from a checkout containing ``omnigent/`` still
  imports the release venv's package (i.e. cwd cannot shadow
  site-packages);
* running from the release source root cannot hide a missing/broken
  installed package;
* an editable finder pointing at the main checkout fails;
* a venv symlink to another checkout fails;
* ``omnigent.server.app`` resolving outside the release's
  site-packages fails;
* missing runtime dependencies fail honestly (the gate doesn't try
  to mask a broken install).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.deploy.supervisor import provenance as prov


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _build_minimal_site_packages(site_packages: Path) -> None:
    """Create a minimal site-packages with omnigent and a server.app stub."""
    site_packages.mkdir(parents=True, exist_ok=True)
    pkg = site_packages / "omnigent"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('VERSION = "0"\n')
    server = pkg / "server"
    server.mkdir()
    (server / "__init__.py").write_text("")
    (server / "app.py").write_text("def main(): return 'app'\n")


def _build_fake_release(
    tmp_path: Path, *, with_editable_finder: bool = False,
    extra_pth_lines: tuple[str, ...] = (),
    module_in_site_packages: bool = True,
    module_in_release_source: bool = False,
    sys_prefix_under_release: bool = True,
) -> Path:
    """Build a release directory with a plausible .venv layout.

    ``with_editable_finder`` injects a ``__editable___omnigent_xxx.pth``
    finder pointing at ``/tmp/<random>/omnigent`` so the
    editable-finder check has something to reject.

    ``extra_pth_lines`` lets a test inject additional ``.pth`` lines
    pointing outside site-packages (e.g. ``/tmp/other``). The pth-check
    must reject every such line.

    ``module_in_release_source`` creates ``<release>/omnigent/__init__.py``
    so we can verify the check refuses to let the release source root
    shadow the site-packages install.
    """
    release = tmp_path / "release"
    venv = release / ".venv"
    (venv).mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /tmp\n")
    site_packages = venv / "lib" / "python3.12" / "site-packages"
    if module_in_site_packages:
        _build_minimal_site_packages(site_packages)
    if module_in_release_source:
        pkg = release / "omnigent"
        pkg.mkdir()
        (pkg / "__init__.py").write_text('VERSION = "shadowed"\n')
        server = pkg / "server"
        server.mkdir()
        (server / "__init__.py").write_text("")
        (server / "app.py").write_text("def main(): return 'shadowed'\n")
    # Inject editable finder pth if requested.
    if with_editable_finder:
        finder_dir = tmp_path / "editable"
        (finder_dir / "omnigent").mkdir(parents=True)
        (finder_dir / "omnigent" / "__init__.py").write_text('VERSION = "shadowed"\n')
        (site_packages / "__editable___omnigent_test_finder.py").write_text(
            "import sys\n"
            "from importlib.machinery import ModuleSpec, PathFinder\n"
            "from pathlib import Path\n"
            f"MAPPING = {{'omnigent': Path({str(finder_dir / 'omnigent')!r})}}\n"
            "class _EditableFinder:\n"
            "    @classmethod\n"
            "    def find_spec(cls, name, path=None, target=None):\n"
            "        if name in MAPPING:\n"
            "            from importlib.util import spec_from_file_location\n"
            "            return spec_from_file_location(name, MAPPING[name] / '__init__.py')\n"
            "        return None\n"
            "sys.meta_path.append(_EditableFinder)\n"
        )
    for line in extra_pth_lines:
        (site_packages / "extra.pth").write_text(line)
    return release


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip inherited PYTHONPATH / repo-root leakage from the test env."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("REPO_ROOT", raising=False)


def _patch_release_paths(
    monkeypatch: pytest.MonkeyPatch,
    release: Path,
    *,
    site_packages: Path,
    omnigent_path: Path,
    app_path: Path,
    executable: Path,
    prefix: Path,
) -> None:
    """Patch the provenance helpers to return the test's controlled paths."""
    monkeypatch.setattr(prov, "_resolve_executable", lambda: executable)
    monkeypatch.setattr(prov, "_resolve_prefix", lambda: prefix)
    monkeypatch.setattr(prov, "_resolve_module",
        lambda name, *, attr=None: omnigent_path if name == "omnigent" else app_path,
    )
    monkeypatch.setattr(prov, "_release_site_packages", lambda _: site_packages)


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------


def test_proper_non_editable_release_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real non-editable install with both module files in site-packages passes."""
    release = _build_fake_release(tmp_path)
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    _patch_release_paths(
        monkeypatch, release,
        site_packages=site_packages,
        omnigent_path=site_packages / "omnigent" / "__init__.py",
        app_path=site_packages / "omnigent" / "server" / "app.py",
        executable=release / ".venv" / "bin" / "python",
        prefix=release,
    )
    info = prov.check_runtime_provenance(release)
    assert info["site_packages"] == str(site_packages)
    assert info["omnigent_module"].endswith("site-packages/omnigent/__init__.py")
    assert info["omnigent_server_app"].endswith("site-packages/omnigent/server/app.py")


def test_module_resolving_from_checkout_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running from a checkout containing ``omnigent/`` must not let the
    checkout shadow the installed wheel. Here we simulate the bypass:
    the ``omnigent`` module path resolves to the *main checkout*
    (i.e. somewhere outside ``<release>/.venv/lib/.../site-packages``).
    """
    release = _build_fake_release(tmp_path)
    # Pretend omnigent resolved from /tmp/elsewhere (the "checkout").
    checkout = tmp_path / "main-checkout" / "omnigent" / "__init__.py"
    checkout.parent.mkdir(parents=True)
    checkout.write_text("")
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    _patch_release_paths(
        monkeypatch, release,
        site_packages=site_packages,
        omnigent_path=checkout,
        app_path=site_packages / "omnigent" / "server" / "app.py",
        executable=release / ".venv" / "bin" / "python",
        prefix=release,
    )
    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(release)
    assert "site-packages" in str(exc.value)


def test_running_from_release_source_root_cannot_hide_missing_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the release has no installed wheel but has ``<release>/omnigent``,
    provenance must still refuse — the release source root is a
    forbidden location. ``module_in_site_packages=False`` removes the
    installed wheel, simulating a broken build; the check must fail
    closed rather than pass because of the bare ``<release>/omnigent``.
    """
    release = _build_fake_release(tmp_path, module_in_site_packages=False,
                                  module_in_release_source=True)
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    release_source_omnigent = release / "omnigent" / "__init__.py"
    _patch_release_paths(
        monkeypatch, release,
        site_packages=site_packages,
        omnigent_path=release_source_omnigent,
        app_path=release / "omnigent" / "server" / "app.py",
        executable=release / ".venv" / "bin" / "python",
        prefix=release,
    )
    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(release)
    # The error message must mention the actual diverging path so the
    # operator can chase the broken build.
    assert "site-packages" in str(exc.value)
    assert exc.value.diverging == release_source_omnigent


def test_editable_finder_pointing_at_checkout_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``__editable__*`` finder pointing at a foreign checkout fails the check.

    Builds a release whose site-packages contains a synthetic
    ``__editable___omnigent_test_finder.py`` and imports it so the
    finder registers in ``sys.meta_path``. The provenance check
    refuses to start under that state.
    """
    release = _build_fake_release(tmp_path, with_editable_finder=True)
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    finder_py = site_packages / "__editable___omnigent_test_finder.py"
    # The synthetic finder module lives under the release's
    # site-packages; import it so the finder registers in sys.meta_path.
    import importlib.util as _ilu
    finder_name = "__editable___omnigent_test_finder"
    spec = _ilu.spec_from_file_location(finder_name, finder_py)
    assert spec and spec.loader
    mod = _ilu.module_from_spec(spec)
    # IMPORTANT: register in sys.modules BEFORE exec_module so the
    # module's __file__ attribute is set after exec. Without this,
    # the loader writes the file's __file__ into the module but the
    # module is not visible to subsequent calls that scan sys.modules.
    sys.modules[finder_name] = mod
    spec.loader.exec_module(mod)
    try:
        _patch_release_paths(
            monkeypatch, release,
            site_packages=site_packages,
            omnigent_path=site_packages / "omnigent" / "__init__.py",
            app_path=site_packages / "omnigent" / "server" / "app.py",
            executable=release / ".venv" / "bin" / "python",
            prefix=release,
        )
        with pytest.raises(prov.ProvenanceError) as exc:
            prov.check_runtime_provenance(release)
        msg = str(exc.value).lower()
        assert "editable" in msg, exc.value
    finally:
        # Drop the finder module so the next test starts clean.
        sys.modules.pop(finder_name, None)


def test_foreign_pth_entry_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.pth`` file under site-packages that points outside the
    release fails — the venv is supposed to be immutable.
    """
    other = tmp_path / "other-checkout"
    other.mkdir()
    release = _build_fake_release(
        tmp_path,
        extra_pth_lines=(str(other),),
    )
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    _patch_release_paths(
        monkeypatch, release,
        site_packages=site_packages,
        omnigent_path=site_packages / "omnigent" / "__init__.py",
        app_path=site_packages / "omnigent" / "server" / "app.py",
        executable=release / ".venv" / "bin" / "python",
        prefix=release,
    )
    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(release)
    assert ".pth" in str(exc.value)


def test_omnigent_server_app_resolving_outside_site_packages_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``omnigent.server.app`` resolving outside the release's site-packages fails."""
    release = _build_fake_release(tmp_path)
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    foreign_app = tmp_path / "foreign" / "app.py"
    foreign_app.parent.mkdir(parents=True)
    foreign_app.write_text("")
    _patch_release_paths(
        monkeypatch, release,
        site_packages=site_packages,
        omnigent_path=site_packages / "omnigent" / "__init__.py",
        app_path=foreign_app,
        executable=release / ".venv" / "bin" / "python",
        prefix=release,
    )
    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(release)
    assert exc.value.diverging == foreign_app
    assert "site-packages" in str(exc.value)


def test_venv_symlink_to_other_checkout_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venv that is a symlink to another checkout's .venv fails."""
    release = tmp_path / "release"
    (release / ".venv").mkdir(parents=True)
    other_venv = tmp_path / "other-checkout" / ".venv"
    other_venv.mkdir(parents=True)
    (other_venv / "pyvenv.cfg").write_text("home = /tmp\n")
    # Replace the release's .venv with a symlink to the other venv.
    shutil.rmtree(release / ".venv")
    (release / ".venv").symlink_to(other_venv)

    monkeypatch.setattr(prov, "_resolve_executable", lambda: release / ".venv" / "bin" / "python")
    monkeypatch.setattr(prov, "_resolve_prefix", lambda: other_venv.resolve())
    monkeypatch.setattr(prov, "_resolve_module", lambda *a, **k: other_venv / "x")
    with pytest.raises(prov.ProvenanceError) as exc:
        prov.check_runtime_provenance(release)
    assert "sys.prefix" in str(exc.value)
    assert exc.value.diverging == other_venv.resolve()


def test_missing_runtime_dependency_fails_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``omnigent`` cannot be imported at all (broken install),
    the check raises a clear error rather than silently passing.
    """
    release = _build_fake_release(tmp_path, module_in_site_packages=False)
    site_packages = release / ".venv" / "lib" / "python3.12" / "site-packages"
    _patch_release_paths(
        monkeypatch, release,
        site_packages=site_packages,
        omnigent_path=site_packages / "omnigent" / "__init__.py",  # doesn't exist
        app_path=site_packages / "omnigent" / "server" / "app.py",
        executable=release / ".venv" / "bin" / "python",
        prefix=release,
    )
    # _resolve_module should raise ImportError when the wheel isn't there;
    # in this test we simulate the "wheel missing" state by raising.
    def broken_resolve(name: str, *, attr: str | None = None) -> Path:
        raise ImportError(f"No module named {name!r}")
    monkeypatch.setattr(prov, "_resolve_module", broken_resolve)
    with pytest.raises(ImportError):
        prov.check_runtime_provenance(release)


def test_cli_refuses_with_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI refuses to run when ``PYTHONPATH`` is set in the env.

    Invoke ``main()`` directly so the argv length check does not
    confuse the test for a CLI-usage error (pytest injects extra
    arguments into ``sys.argv`` when running ``-m pytest``).
    """
    monkeypatch.setenv("PYTHONPATH", "/some/where")
    monkeypatch.setenv("OMNIGENT_RELEASE_DIR", str(tmp_path))
    # Clear sys.argv so the length check doesn't accidentally reject us.
    monkeypatch.setattr(sys, "argv", ["provenance"])
    rc = prov.main()
    assert rc == 1