"""Systemd helpers for the long-term deploy architecture.

The live service unit is intentionally long-lived: it references the
``OMNIGENT_RELEASE_DIR`` env var (which itself is set by the active
``10-release-<sha>.conf`` drop-in) rather than hard-coding a worktree
path. This module writes that drop-in and small helpers around it.

The drop-in replaces the old ``10-deploy-main-<sha>.conf`` workflow
that pointed the unit at a per-SHA worktree. The new pattern keeps the
unit itself stable: only the drop-in changes between promotions, and
``current`` symlink swaps are reflected in the drop-in by the next
``promote_release`` invocation.

The promotion script writes the drop-in to a temporary file under the
drop-in directory's own ``.tmp`` suffix and atomically renames it —
this prevents systemd from picking up a half-written file during a
``daemon-reload`` triggered by a concurrent agent.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


_DEFAULT_SERVICE = "omnigent-eval-web.service"
_DEFAULT_DROPIN_DIR = Path("/etc/systemd/system/omnigent-eval-web.service.d")
_DEFAULT_PORT = 4097
_DEFAULT_CONFIG = Path("/home/hermes/.omnigent/config.yaml")


class SystemdError(RuntimeError):
    """Raised when the systemd drop-in cannot be written.

    The promotion script calls the entry point from a normal shell
    via ``sudo`` because the drop-in directory is owned by ``root``.
    A failure here is normally a permissions issue; the operator
    sees the path and ``ls -ld`` output in the error message.
    """


def _resolve(value: str | None, *, fallback: str) -> str:
    raw = (value or "").strip()
    return raw or fallback


def service_name() -> str:
    return _resolve(os.environ.get("OMNIGENT_DEPLOY_SERVICE_NAME"), fallback=_DEFAULT_SERVICE)


def dropin_dir() -> Path:
    raw = os.environ.get("OMNIGENT_DEPLOY_DROPIN_DIR", "").strip()
    return Path(raw) if raw else _DEFAULT_DROPIN_DIR


def service_port() -> int:
    raw = os.environ.get("OMNIGENT_DEPLOY_SERVICE_PORT", "").strip()
    try:
        return int(raw) if raw else _DEFAULT_PORT
    except ValueError:
        return _DEFAULT_PORT


def config_path() -> Path:
    raw = os.environ.get("OMNIGENT_DEPLOY_CONFIG_PATH", "").strip()
    return Path(raw) if raw else _DEFAULT_CONFIG


def write_release_dropin(sha: str, *, release_dir: Path) -> Path:
    """Write ``10-release-<short-sha>.conf`` for the given release.

    The drop-in sets ``Environment=OMNIGENT_RELEASE_DIR=...`` and
    ``Environment=OMNIGENT_RELEASE_EXPECTED_SHA=...`` *and* the
    ``WorkingDirectory`` and ``ExecStart`` pointing at the release's
    own ``.venv/bin/python``. systemd drop-ins replace ``ExecStart``
    in the base unit when they set it explicitly (empty string then
    full value), so this drop-in takes ownership of the command line.

    Most importantly, the drop-in installs an ``ExecStartPre=`` that
    runs the supervisor gate as the release's own Python interpreter.
    The gate verifies provenance, manifest SHA, and (unless explicit
    API-only) the web UI bundle. A failure aborts the systemd startup
    with a runbook-quality message in the journal; the service cannot
    come up under a misconfigured release.

    Note: systemd does NOT expand ``%E`` / env-var references inside
    ``ExecStart`` (only inside ``EnvironmentFile=-`` and a few
    directives). For that reason this drop-in writes the resolved
    release path directly into both ``ExecStartPre`` and ``ExecStart``
    — there is no template indirection that an LLM agent could
    mis-target.

    :param sha: Full release SHA (used in the drop-in filename and
        embedded inside the file as documentation).
    :param release_dir: Absolute path of the release directory.
    :returns: Path of the drop-in file that was written.
    """
    target = dropin_dir() / f"10-release-{sha[:12]}.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    config = config_path()
    port = service_port()
    body = (
        f"# Pin omnigent-eval-web at release {sha}\n"
        f"# using {release_dir} (release-local .venv, immutable).\n"
        f"# Drop-in precedence (10-release-*) wins over the older deploy-main-*\n"
        f"# drop-ins and over pre-existing evaluator/route-approval/router/\n"
        f"# tailscale-origin drop-ins; their Environment*= lines stay in place.\n"
        f"[Service]\n"
        f"WorkingDirectory={release_dir}\n"
        f"# Supervisor pre-start gate. Runs as the release's own Python so\n"
        f"# ``omnigent`` imports from this release's venv (not from the main\n"
        f"# checkout). Aborts systemd startup on failure.\n"
        f"ExecStartPre=-{release_dir}/.venv/bin/python -m omnigent.deploy.supervisor.gate\n"
        f"ExecStart=\n"
        f"ExecStart={release_dir}/.venv/bin/python -m omnigent server \\\n"
        f"  --host 127.0.0.1 --port {port} \\\n"
        f"  --no-open \\\n"
        f"  --config {config}\n"
        f"Environment=OMNIGENT_RELEASE_DIR={release_dir}\n"
        f"Environment=OMNIGENT_RELEASE_EXPECTED_SHA={sha}\n"
    )
    _atomic_write(target, body)
    return target


def disable_other_release_dropins(active_sha: str) -> list[Path]:
    """Move any ``10-release-*.conf`` not matching ``active_sha`` to
    ``.disabled``.

    Mirrors the older ``promote_main_deploy.sh`` cleanup so the drop-in
    precedence resolves to the new release deterministically, even when
    a sequence of promotion attempts left stale drop-ins behind.
    Returns the list of disabled drop-ins (informational).
    """
    disabled: list[Path] = []
    active_name = f"10-release-{active_sha[:12]}.conf"
    for entry in dropin_dir().iterdir():
        if not entry.is_file():
            continue
        if entry.suffix == ".disabled":
            continue
        if not entry.name.startswith("10-release-") or not entry.name.endswith(".conf"):
            continue
        if entry.name == active_name:
            continue
        target = entry.with_suffix(entry.suffix + ".disabled")
        shutil.move(str(entry), str(target))
        disabled.append(target)
    return disabled


def _atomic_write(target: Path, body: str) -> None:
    """Atomic write: tempfile in the parent dir, fsync, rename.

    Used so a crashed promotion script does not leave a half-written
    drop-in on disk that systemd picks up on the next daemon-reload.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.tmp.",
        text=False,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
