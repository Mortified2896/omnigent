"""Filesystem layout for the long-term deploy architecture.

The canonical layout is::

    <deploy_root>/
        releases/<full-sha>/       # immutable release dirs
            .venv/                 # release-local frozen venv
            omnigent/...           # source tree (extracted by git archive)
            web/                   # source tree (npm ci + npm run build
                                   #   populate ../omnigent/server/static/web-ui/)
            manifest.json
            canary/                # ephemeral canary artifacts
        manifests/<full-sha>.json  # copies of release manifests
        failed/<full-sha>/         # failed build artifacts + diagnostics
        current -> releases/<sha>  # active release symlink
        previous -> releases/<old> # last known-good release symlink

Anything outside this tree (the main checkout, ``.venv/`` symlinks,
random worktrees) is intentionally not part of the production runtime;
that's the whole point of the layout.

The defaults are tuned for the deploy host documented in
``docs/deployments/ops.md`` (``/home/hermes/workspace/deployments/omnigent``),
overridable via the ``OMNIGENT_DEPLOY_ROOT`` env var so the supervisor
can be smoke-tested in tmp paths under pytest.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_DEPLOY_ROOT = Path("/home/hermes/workspace/deployments/omnigent")
_ENV_DEPLOY_ROOT = "OMNIGENT_DEPLOY_ROOT"


def deploy_root() -> Path:
    """Return the configured deploy root (creating it lazily when missing).

    The deploy root is created with ``0o755`` permissions because the
    promoting user (the user who runs the script) must be able to write
    into ``releases/`` and ``failed/``; the live service reads
    ``current`` and the release's ``.venv`` but does not need write
    access there. The promotion scripts do this via ``sudo`` rather
    than running as the service user, which is cleaner to reason about.
    """
    raw = os.environ.get(_ENV_DEPLOY_ROOT, "").strip()
    root = Path(raw) if raw else _DEFAULT_DEPLOY_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def releases_dir() -> Path:
    """The ``releases/`` subdir of the deploy root."""
    p = deploy_root() / "releases"
    p.mkdir(parents=True, exist_ok=True)
    return p


def manifests_dir() -> Path:
    """The ``manifests/`` subdir of the deploy root."""
    p = deploy_root() / "manifests"
    p.mkdir(parents=True, exist_ok=True)
    return p


def failed_dir() -> Path:
    """The ``failed/`` subdir of the deploy root. Holds diagnostics
    from rejected candidate releases."""
    p = deploy_root() / "failed"
    p.mkdir(parents=True, exist_ok=True)
    return p


def current_link() -> Path:
    """The ``current`` symlink that points at the active release."""
    return deploy_root() / "current"


def previous_link() -> Path:
    """The ``previous`` symlink that points at the previous known-good
    release. Used by rollback and by the status command."""
    return deploy_root() / "previous"


def release_dir_for(sha: str) -> Path:
    """The immutable directory for ``sha``. Refuses non-canonical SHAs."""
    return releases_dir() / sha


def manifest_path_for_sha(sha: str) -> Path:
    """Path of the archived manifest under ``manifests/<sha>.json``."""
    return manifests_dir() / f"{sha}.json"


def failed_dir_for(sha: str) -> Path:
    """Path of the diagnostics dir for a failed release."""
    return failed_dir() / sha


def safe_resolve(path: Path) -> Path:
    """Resolve ``path`` strictly inside the deploy root.

    Used by cleanup logic so a symlink escape (e.g. ``releases/foo ->
    /etc``) cannot make ``shutil.rmtree`` walk outside the deploy
    tree. Returns the resolved path; raises ``ValueError`` on escape.
    """
    root = deploy_root().resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path {path} resolves outside deploy root {root}")
    return resolved
