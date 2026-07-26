"""Deployment-time helpers for the `omnigent-eval-web` systemd service.

The Tailscale-public Omnigent instance is a systemd unit that runs from a
detached git worktree (``deploy-main-<sha>``). The web UI is built into the
worktree's ``omnigent/server/static/web-ui/`` directory by `npm run build` —
when that bundle is missing the server falls back to the API-only landing
page at ``/``, which is the single most common "the web UI doesn't load"
report.

This package isolates the checks that the deploy promotion script and the
server startup share. The deploy script runs them as a preflight before
promoting a new worktree, and the server runs them at startup so that a
silent API-only fallback on a normal UI deployment is impossible (the
server logs a loud ERROR that systemd captures in the unit journal).
"""

# Symbols are intentionally re-exported here for callers that prefer the
# short path (``from omnigent.deploy import verify_web_ui_bundle``). The
# underlying module is also a CLI entry point (`python -m
# omnigent.deploy.preflight`) so the import is gated behind __getattr__
# to keep ``python -m omnigent.deploy.preflight`` free of the
# "found in sys.modules" RuntimeWarning that eager parent-package
# imports trigger under runpy.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.deploy.preflight import (
        WebUIBundleMissingError,
        expected_web_ui_index,
        is_api_only_deployment,
        startup_web_ui_check,
        verify_web_ui_bundle,
    )

__all__ = [
    "WebUIBundleMissingError",
    "expected_web_ui_index",
    "is_api_only_deployment",
    "startup_web_ui_check",
    "verify_web_ui_bundle",
]

_LAZY_MAP = {
    "WebUIBundleMissingError": "omnigent.deploy.preflight",
    "expected_web_ui_index": "omnigent.deploy.preflight",
    "is_api_only_deployment": "omnigent.deploy.preflight",
    "startup_web_ui_check": "omnigent.deploy.preflight",
    "verify_web_ui_bundle": "omnigent.deploy.preflight",
}


def __getattr__(name: str):  # pragma: no cover - lazy import shim
    module_name = _LAZY_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module 'omnigent.deploy' has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
