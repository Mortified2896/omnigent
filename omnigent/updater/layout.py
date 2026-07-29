"""Filesystem layout for the external updater (issue #38).

The updater's state root, deploy root, and repository root are
configurable via environment variables so tests and staging never
touch production paths. Defaults follow the project conventions
documented in :mod:`omnigent.deploy.ops.layout` and issue #38.

The state root is created lazily with ``0o755`` permissions because
the updater process must be able to read its durable state while
running as a system service. ``requests/``, ``running/``,
``results/``, ``events/``, ``locks/``, and ``maintenance.json`` are
all created on first access.

The state root is **outside** the deploy root by design — a deploy
rollback cannot touch the updater's records.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_STATE_ROOT = Path("/var/lib/omnigent/updates")
_DEFAULT_DEPLOY_ROOT = Path("/home/hermes/workspace/deployments/omnigent")
_DEFAULT_REPO_ROOT = Path("/home/hermes/workspace/repos/omnigent-eval")
_DEFAULT_LINEAGE_ANCHOR = "c1f23749c4dd0b24ce62a17d926b9660bf99db5c"
_DEFAULT_LIVE_DEPLOYED_SHA = Path("/home/hermes/.omnigent/deployed-sha")
_DEFAULT_NOTIFY_PORT = 4097
_ENV_STATE_ROOT = "OMNIGENT_UPDATER_STATE_ROOT"
_ENV_DEPLOY_ROOT = "OMNIGENT_UPDATER_DEPLOY_ROOT"
_ENV_REPO_ROOT = "OMNIGENT_UPDATER_REPO_ROOT"
_ENV_LINEAGE_ANCHOR = "OMNIGENT_UPDATER_LINEAGE_ANCHOR"
_ENV_LIVE_SHA = "OMNIGENT_UPDATER_LIVE_SHA_FILE"
_ENV_NOTIFY_PORT = "OMNIGENT_UPDATER_NOTIFY_PORT"


def _resolve(value: str | None, *, fallback: Path | str) -> str:
    raw = (value or "").strip()
    return raw or str(fallback)


def state_root() -> Path:
    """Return the configured updater state root, creating it lazily."""
    raw = os.environ.get(_ENV_STATE_ROOT, "").strip()
    root = Path(raw) if raw else _DEFAULT_STATE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def deploy_root() -> Path:
    """Return the configured deploy root (mirrors ``omnigent.deploy.ops.layout``).

    Defers to the production deploy helper so the same paths and
    laziness rules apply to both the updater and the rest of the
    deployment code.
    """
    from omnigent.deploy.ops import layout as deploy_layout

    return deploy_layout.deploy_root()


def repo_root() -> Path:
    """Return the configured repository root (the source tree the
    updater builds from).

    The updater always reads from a real git checkout rather than a
    release dir, so it can run ``git archive`` / ``git cat-file`` on
    the immutable fork/main lineage without depending on the live
    release.
    """
    raw = os.environ.get(_ENV_REPO_ROOT, "").strip()
    root = Path(raw) if raw else _DEFAULT_REPO_ROOT
    if not root.is_dir():
        raise FileNotFoundError(f"OMNIGENT_UPDATER_REPO_ROOT does not exist: {root}")
    return root


def requests_dir() -> Path:
    p = state_root() / "requests"
    p.mkdir(parents=True, exist_ok=True)
    return p


def running_dir() -> Path:
    p = state_root() / "running"
    p.mkdir(parents=True, exist_ok=True)
    return p


def results_dir() -> Path:
    p = state_root() / "results"
    p.mkdir(parents=True, exist_ok=True)
    return p


def events_dir() -> Path:
    p = state_root() / "events"
    p.mkdir(parents=True, exist_ok=True)
    return p


def locks_dir() -> Path:
    p = state_root() / "locks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def rehearsal_dir() -> Path:
    """Per-request migration-rehearsal scratch space."""
    p = state_root() / "rehearsal"
    p.mkdir(parents=True, exist_ok=True)
    return p


def backups_dir() -> Path:
    """Per-request database backups created immediately before cutover."""
    p = state_root() / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def pending_deliveries_dir() -> Path:
    """Result files that the web service hasn't acknowledged yet.

    The web service reconciles this directory on startup and deletes
    each file after a successful ``POST /api/updater/result-deliver``.
    """
    p = state_root() / "pending-deliveries"
    p.mkdir(parents=True, exist_ok=True)
    return p


def maintenance_marker_path() -> Path:
    """The durable maintenance-mode marker file the web service reads.

    Lives outside the deploy root so a release rollback cannot
    silently clear it. The file holds a JSON document of the form::

        {"active": true, "request_id": "<id>", "set_at": "<iso>"}

    The web service treats maintenance as active when the file
    exists and ``active`` is true.
    """
    return state_root() / "maintenance.json"


def request_path(request_id: str) -> Path:
    """Path of the request record file. Atomic create in
    :func:`omnigent.updater.store.create_request`."""
    return requests_dir() / f"{request_id}.json"


def running_path(request_id: str) -> Path:
    """Path of the per-request checkpoint file.

    A request's running file is created when the controller first
    transitions out of ``queued`` and is removed once the request
    reaches a terminal state. Existence of the file is the
    authoritative "this request is in flight" signal.
    """
    return running_dir() / f"{request_id}.json"


def result_path(request_id: str) -> Path:
    """Path of the terminal result record."""
    return results_dir() / f"{request_id}.json"


def events_path(request_id: str) -> Path:
    """Append-only JSONL event log for one request."""
    return events_dir() / f"{request_id}.jsonl"


def lock_path(request_id: str) -> Path:
    """Per-request single-active-update lock file.

    Held with :class:`omnigent.updater.locking.UpdateLock`. The lock
    itself is what guarantees "only one production update may be
    active at a time".
    """
    return locks_dir() / f"{request_id}.lock"


def lineage_anchor() -> str:
    """Configured lineage anchor SHA used for ancestry checks.

    The updater requires every target SHA to be a descendant of this
    anchor (via ``git merge-base --is-ancestor``). The default is
    the post-#38 lineage head agreed in fork/main; operators can
    override via ``OMNIGENT_UPDATER_LINEAGE_ANCHOR`` to roll the
    anchor forward after a future migration joins the lineage.
    """
    return _resolve(
        os.environ.get(_ENV_LINEAGE_ANCHOR),
        fallback=_DEFAULT_LINEAGE_ANCHOR,
    )


def live_sha_file() -> Path:
    """Path of the file that records the live deployed SHA.

    Mirrors ``scripts/promote_release.sh``'s ``deployed-sha``
    semantics: the file is rewritten only after the live health
    probes pass.
    """
    raw = os.environ.get(_ENV_LIVE_SHA, "").strip()
    return Path(raw) if raw else _DEFAULT_LIVE_DEPLOYED_SHA


def notify_port() -> int:
    """Configured web service loopback port for delivery + status calls."""
    raw = os.environ.get(_ENV_NOTIFY_PORT, "").strip()
    try:
        return int(raw) if raw else _DEFAULT_NOTIFY_PORT
    except ValueError:
        return _DEFAULT_NOTIFY_PORT
