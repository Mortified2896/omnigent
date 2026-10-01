"""Regression test for the omnigent.llms <-> omnigent.util.reasoning_effort cycle.

The eager top-level imports in ``omnigent/llms/__init__.py`` created
a circular load when any caller imported ``omnigent.llms.errors``
during the load of ``omnigent.util.reasoning_effort`` (which happens on
every server-routes import via ``server/routes/sessions.py``).

The fix in ``omnigent/llms/__init__.py`` switches to a
``__getattr__`` shim so ``Client`` and ``get_model_context_window``
are resolved lazily on first access. This test guards against
re-introducing the cycle by re-importing the affected modules
in a fresh interpreter-style namespace and asserting both the
short-form and long-form import paths work.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def _run_fresh(source: str) -> None:
    """Exercise cold imports without invalidating modules held by other tests."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_sessions_routes_import_does_not_trigger_cycle() -> None:
    """The original failure shape: importing the server routes module
    triggered ``reasoning_effort`` -> ``llms.errors`` -> ``llms.__init__``
    -> ``llms.client`` -> ``reasoning_effort`` re-entry."""
    _run_fresh("import omnigent.server.routes.sessions")


def test_short_form_import_still_works() -> None:
    """The public short-form imports remain available after the lazy switch."""
    _run_fresh("""
        from omnigent.llms import Client, get_model_context_window
        assert Client is not None
        assert callable(get_model_context_window)
    """)


def test_module_only_import_does_not_load_client() -> None:
    """Importing the package alone must not eagerly import the client."""
    _run_fresh("""
        import sys
        import omnigent.llms
        assert "omnigent.llms.client" not in sys.modules, (
            "omnigent.llms.client was imported eagerly; lazy shim regressed"
        )
    """)


def test_unknown_attribute_raises_attribute_error() -> None:
    """Unknown attributes retain the normal AttributeError contract."""
    _run_fresh("""
        import omnigent.llms as llms_pkg
        try:
            llms_pkg.does_not_exist
        except AttributeError as e:
            assert "does_not_exist" in str(e)
        else:
            raise AssertionError("expected AttributeError")
    """)
