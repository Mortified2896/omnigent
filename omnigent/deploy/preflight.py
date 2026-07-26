"""Preflight checks for the deploy-main-* promotion workflow.

The deploy promotion script (``scripts/promote_main_deploy.sh``) and the
server startup (``omnigent/server/app.py``) share two facts:

- The web UI bundle lives at ``<worktree>/omnigent/server/static/web-ui/index.html``.
- An API-only deployment is opt-in via the ``OMNIGENT_SKIP_WEB_UI`` env var.

Without these checks, a deploy-main-* worktree that forgot to run
``npm ci && npm run build`` would silently come up as an API-only server.
The python-side check is a loud ERROR log; the deploy-script-side check
is a hard preflight that aborts the promotion before systemd reloads.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Env var opt-in for an API-only deployment. Mirrors the same name used by
# ``setup.py:_build_web_ui`` so the wheel-build path and the deployment path
# agree on the same signal. The full string ``"true"`` (case-insensitive)
# is the only accepted value; an empty/unset env var means "this is a UI
# deployment, the bundle is required".
_API_ONLY_ENV_VAR = "OMNIGENT_SKIP_WEB_UI"

# Files inside the worktree that confirm a successful frontend build.
# ``index.html`` alone is necessary; the hashing fingerprint and assets dir
# are the cheat-sheet that the bundler produced traversed the full Vite
# pipeline. ``version.json`` is the cheapest stable artifact to probe.
_FRONTEND_BUILD_ARTIFACTS = ("index.html", "version.json", "manifest.webmanifest")


class WebUIBundleMissingError(RuntimeError):
    """Raised when a deploy-main-* worktree lacks a built web UI bundle.

    The error message is a runbook: it tells the operator exactly which
    commands to run to rebuild the bundle, so the failure is actionable
    rather than a fog of "the UI doesn't load".
    """

    def __init__(self, worktree_root: Path, missing: tuple[str, ...]) -> None:
        rel = ", ".join(missing)
        super().__init__(
            f"web UI bundle missing under {worktree_root}/omnigent/server/static/web-ui/ "
            f"(missing: {rel}). Rebuild the bundle before promoting this worktree:\n"
            f"  cd {worktree_root}/web\n"
            f"  npm ci   # or `npm install` if the lockfile is out of sync\n"
            f"  npm run build   # writes to ../omnigent/server/static/web-ui\n"
            f"Then re-run scripts/promote_main_deploy.sh.\n"
            f"If this is an intentional API-only deployment, set "
            f"{_API_ONLY_ENV_VAR}=true in the systemd unit environment."
        )
        self.worktree_root = worktree_root
        self.missing = missing


def expected_web_ui_dir(worktree_root: Path) -> Path:
    """Return the absolute path of the expected web UI bundle directory.

    :param worktree_root: Absolute path of the deploy-main-* worktree root.
    :returns: Path to ``omnigent/server/static/web-ui/`` inside the worktree.
    """
    return worktree_root / "omnigent" / "server" / "static" / "web-ui"


def expected_web_ui_index(worktree_root: Path) -> Path:
    """Return the absolute path of ``web-ui/index.html``.

    This is the canonical preflight file: the SPA fallback in
    ``omnigent/server/app.py`` checks the same path (``_WEB_UI_DIST``).

    :param worktree_root: Absolute path of the deploy-main-* worktree root.
    :returns: Path to ``omnigent/server/static/web-ui/index.html``.
    """
    return expected_web_ui_dir(worktree_root) / "index.html"


def is_api_only_deployment() -> bool:
    """Return whether this process is an explicit API-only deployment.

    Reads ``OMNIGENT_SKIP_WEB_UI``; the value is true only when the env var
    is set to a recognised truthy string (``"true"``, ``"1"``, ``"yes"``,
    case-insensitive, whitespace-trimmed). Empty / unset / any other value
    is treated as "this is a normal UI deployment".

    :returns: True if the process is an intentional API-only deployment.
    """
    raw = os.environ.get(_API_ONLY_ENV_VAR, "")
    return raw.strip().lower() in ("1", "true", "yes")


def verify_web_ui_bundle(worktree_root: Path) -> None:
    """Raise :class:`WebUIBundleMissingError` if the bundle is missing.

    The deploy promotion script calls this before writing the systemd
    drop-in or restarting the service. Failing here aborts the promotion
    so that the previous healthy deployment keeps serving traffic.

    :param worktree_root: Absolute path of the worktree to validate.
    :raises WebUIBundleMissingError: If any of the expected bundle
        artifacts is missing from the worktree.
    """
    bundle_dir = expected_web_ui_dir(worktree_root)
    missing: list[str] = []
    for artifact in _FRONTEND_BUILD_ARTIFACTS:
        if not (bundle_dir / artifact).is_file():
            missing.append(artifact)
    if missing:
        raise WebUIBundleMissingError(worktree_root, tuple(missing))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the deploy preflight.

    Used by ``scripts/promote_main_deploy.sh`` before reloading systemd.
    Exits 0 if the bundle is present, 1 (with a diagnostic on stderr) if
    it is missing.

    :param argv: Command-line arguments. Defaults to ``sys.argv[1:]``.
    :returns: Exit code (0 = bundle present, 1 = bundle missing).
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(f"usage: {sys.argv[0]} <worktree-root>", file=sys.stderr)
        return 2
    worktree_root = Path(args[0]).resolve()
    try:
        verify_web_ui_bundle(worktree_root)
    except WebUIBundleMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"web UI bundle OK: {expected_web_ui_index(worktree_root)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())


def startup_web_ui_check(
    web_ui_dist: Path | None,
    *,
    worktree_root: Path | None = None,
) -> bool:
    """Log a loud ERROR if the running server is silently API-only.

    Called by ``omnigent/server/app.py`` at startup. The check is purely
    advisory (raises no exception) so the existing API-only fallback path
    in ``app.py`` keeps working for intentional API-only deployments, but
    a normal UI deployment that comes up without the bundle can no longer
    do so silently — systemd captures the ERROR line in the unit journal
    and the deploy promotion script also fails the preflight before
    reload.

    :param web_ui_dist: Resolved web UI bundle directory (the same path
        ``app.py`` checks at startup). If ``None`` the loud-ERROR is
        suppressed because the test fixtures that monkeypatch
        ``_WEB_UI_DIST`` deliberately point it at non-existent paths
        and the loud-ERROR would add noise to every unit test.
    :param worktree_root: Optional worktree root for runbook context in
        the log message. Falls back to deriving it from ``web_ui_dist``
        when not supplied.
    :returns: True if the bundle is present, False if the server is
        about to serve the API-only fallback on a normal UI deployment.
    """
    if web_ui_dist is None:
        return True
    bundle_ok = (web_ui_dist / "index.html").is_file()
    if bundle_ok or is_api_only_deployment():
        return bundle_ok
    runbook_root = worktree_root if worktree_root is not None else web_ui_dist.parent.parent.parent
    message = (
        f"omnigent-server: web UI bundle missing at {web_ui_dist / 'index.html'}. "
        "The server will fall back to the API-only landing page at '/'. This is "
        "the symptom of a deploy-main-* worktree that forgot to run "
        "`npm ci && npm run build` before promotion. To rebuild:\n"
        f"  cd {runbook_root}/web\n"
        "  npm ci   # or `npm install` if the lockfile is out of sync\n"
        "  npm run build\n"
        "Then re-run scripts/promote_main_deploy.sh (or restart the "
        "service). If this is an intentional API-only deployment, set "
        "OMNIGENT_SKIP_WEB_UI=true in the systemd unit environment."
    )
    # Also write directly to stderr so the line is captured by the
    # systemd unit journal even when uvicorn's ``log_config`` does not
    # attach a handler to the root logger (which it doesn't — it only
    # configures the uvicorn.* loggers). Without this fallback the
    # ``_LOGGER.error`` call below is silently dropped in production
    # and the loud-ERROR signal fails its job.
    import sys

    print(f"[ERROR] {message}", file=sys.stderr)
    _LOGGER.error(message)
    return False
