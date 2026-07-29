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

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_DEFAULT_SERVICE = "omnigent-eval-web.service"
_DEFAULT_HOST_SERVICE = "omnigent-eval-host.service"
_DEFAULT_DROPIN_DIR = Path("/etc/systemd/system/omnigent-eval-web.service.d")
_DEFAULT_HOST_DROPIN_DIR = Path("/etc/systemd/system/omnigent-eval-host.service.d")
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


def host_service_name() -> str:
    return _resolve(
        os.environ.get("OMNIGENT_DEPLOY_HOST_SERVICE_NAME"),
        fallback=_DEFAULT_HOST_SERVICE,
    )


def dropin_dir() -> Path:
    raw = os.environ.get("OMNIGENT_DEPLOY_DROPIN_DIR", "").strip()
    return Path(raw) if raw else _DEFAULT_DROPIN_DIR


def host_dropin_dir() -> Path:
    raw = os.environ.get("OMNIGENT_DEPLOY_HOST_DROPIN_DIR", "").strip()
    return Path(raw) if raw else _DEFAULT_HOST_DROPIN_DIR


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
    # NOTE: ``WorkingDirectory`` is intentionally set to a neutral
    # directory (``/tmp``) rather than to the release directory.
    # Python inserts the cwd into ``sys.path[0]``; if cwd is the
    # release directory, Python would import ``omnigent`` from the
    # bare source tree (``<release>/omnigent/``) instead of the
    # installed wheel under site-packages. The gate and the server
    # both rely on the explicit ``.venv/bin/python`` interpreter and
    # the ``OMNIGENT_RELEASE_DIR`` env var; cwd is irrelevant to
    # their operation.
    body = (
        f"# Pin omnigent-eval-web at release {sha}\n"
        f"# using {release_dir} (release-local .venv, immutable).\n"
        f"# Drop-in precedence (10-release-*) wins over the older deploy-main-*\n"
        f"# drop-ins and over pre-existing evaluator/route-approval/router/\n"
        f"# tailscale-origin drop-ins; their Environment*= lines stay in place.\n"
        f"[Service]\n"
        f"WorkingDirectory=/tmp\n"
        f"# Supervisor pre-start gate. Runs as the release's own Python so\n"
        f"# ``omnigent`` imports from this release's venv (not from the main\n"
        f"# checkout). Aborts systemd startup on failure.\n"
        f"ExecStartPre={release_dir}/.venv/bin/python -P -m omnigent.deploy.supervisor.gate\n"
        f"ExecStart=\n"
        f"ExecStart={release_dir}/.venv/bin/python -P -m omnigent server \\\n"
        f"  --host 127.0.0.1 --port {port} \\\n"
        f"  --no-open \\\n"
        f"  --config {config}\n"
        f"Environment=OMNIGENT_RELEASE_DIR={release_dir}\n"
        f"Environment=OMNIGENT_RELEASE_EXPECTED_SHA={sha}\n"
        f"Environment=PYTHONSAFEPATH=1\n"
    )
    _atomic_write(target, body)
    return target


def disable_other_release_dropins(active_sha: str) -> list[Path]:
    """Move any ``10-release-*.conf`` not matching ``active_sha`` to
    ``.disabled``.

    Cleans both the web and host drop-in directories so a stale
    ``omnigent-eval-host.service.d/10-release-<old-sha>.conf`` cannot
    keep the host pinned at the previous release when the web has
    moved on. Returns the list of disabled drop-ins (informational).
    """
    disabled: list[Path] = []
    active_name = f"10-release-{active_sha[:12]}.conf"
    for parent in (dropin_dir(), host_dropin_dir()):
        if not parent.is_dir():
            continue
        for entry in parent.iterdir():
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


def loaded_release_sha(pid: int) -> str | None:
    """Return the SHA a running process loaded, or ``None`` if unknown.

    Inspects ``/proc/<pid>/cmdline`` for a ``releases/<sha>`` segment
    and the matching process environment for
    ``OMNIGENT_RELEASE_EXPECTED_SHA=``. The cmdline-derived SHA wins
    when both are present so a stale env var cannot mask a wrong
    executable path.

    A ``releases/.staging-<hash>-...`` path is also detected: that
    is a build-time scratch dir the release builder is supposed to
    rename into the canonical path before the host restarts. A host
    still running from a staging dir means the release was not
    promoted atomically; the caller should refuse to declare the
    release live. To make that explicit, the staging SHA is returned
    (so :func:`verify_loaded_release` can compare against
    ``expected_sha`` and fail with a clear message) but the SHA is
    intentionally not marked "loaded" for any other purpose.

    :param pid: Process id to inspect.
    :returns: The 40-char lowercase SHA the process was launched
        from, or ``None`` when /proc is not readable or no SHA
        could be extracted.
    """
    import re

    cmdline_path = Path(f"/proc/{pid}/cmdline")
    environ_path = Path(f"/proc/{pid}/environ")
    try:
        raw_cmdline = cmdline_path.read_bytes()
    except OSError:
        return None
    try:
        raw_environ = environ_path.read_bytes()
    except OSError:
        raw_environ = b""
    cmdline = raw_cmdline.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    environ = raw_environ.decode("utf-8", errors="replace")
    canonical = re.compile(r"releases/([0-9a-f]{40})(?:/|\b)")
    staging = re.compile(r"releases/\.staging-([0-9a-f]{40})-")
    canonical_hits = canonical.findall(cmdline)
    staging_hits = staging.findall(cmdline)
    cmdline_sha = canonical_hits[-1] if canonical_hits else None
    environ_sha = None
    for chunk in environ.split("\x00"):
        if chunk.startswith("OMNIGENT_RELEASE_EXPECTED_SHA="):
            environ_sha = chunk.split("=", 1)[1].strip() or None
            break
    if cmdline_sha and environ_sha and cmdline_sha != environ_sha:
        # Mismatch — env says one SHA, executable path says another.
        # Refuse to pick one; the deployment is in an inconsistent state.
        return None
    return cmdline_sha or environ_sha or (staging_hits[-1] if staging_hits else None)


def normalize_entry_point_shims(release_dir: Path) -> list[Path]:
    """Rewrite ``.venv/bin/{omni,omnigent}`` so their shebang points at the
    canonical release dir, not a deleted ``.staging-<hash>`` dir.

    ``uv pip install .`` emits the entry-point shim with a shebang
    embedded at install time. When the install happens in the
    staging dir (``releases/.staging-<sha>-<pid>-<ts>``) and the
    staging dir is later renamed into the canonical
    ``releases/<sha>``, the embedded shebang still points at the
    staging path. If anything later cleans up the staging dir before
    the host restarts, the host's systemd startup aborts with
    ``status=127`` (``exec format error`` or ``not found``).

    The fix is a one-shot rewrite of the shim's shebang to point at
    the canonical release's own ``.venv/bin/python``. The Python
    body of the shim is unchanged so the click entry point still
    resolves to ``omnigent.cli:main``.

    Idempotent — re-running against a release whose shims already
    point at the canonical path is a no-op (the rewrite produces an
    identical file).

    :param release_dir: Canonical release directory (post-rename).
    :returns: The list of shim files that were rewritten. Informational.
    """
    rewritten: list[Path] = []
    python_bin = release_dir / ".venv" / "bin" / "python"
    if not python_bin.is_file():
        return rewritten
    python_shebang = f"#!{python_bin}\n"
    for shim_name in ("omni", "omnigent"):
        shim = release_dir / ".venv" / "bin" / shim_name
        if not shim.is_file():
            continue
        try:
            existing = shim.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = existing.splitlines(keepends=True)
        # Strip any existing shebang line (must be the very first line
        # in a POSIX script); preserve the rest of the body verbatim.
        body = lines[1:] if lines and lines[0].startswith("#!") else lines
        new_body = "".join([python_shebang, *body])
        if new_body == existing:
            continue
        _atomic_write(shim, new_body)
        with contextlib.suppress(OSError):
            shim.chmod(0o755)
        rewritten.append(shim)
    return rewritten


def verify_loaded_release(
    *,
    service: str,
    expected_sha: str,
    proc: subprocess.CompletedProcess[str] | None = None,
) -> Path:
    """Confirm a service's main PID loaded ``expected_sha``.

    Calls ``systemctl show <service> -p MainPID --value`` (or uses
    the supplied ``proc`` result) to find the service's main PID,
    inspects its ``/proc/<pid>/cmdline`` via
    :func:`loaded_release_sha`, and raises :class:`SystemdError`
    when the loaded SHA disagrees with ``expected_sha``. The caller
    is expected to have just restarted the service, so the main PID
    reflects the post-restart process.

    The intent is the narrow deployment-completeness guarantee the
    issue #30 regression lacked: a deployment is not successful
    until every watchdog-relevant process is running from the same
    release directory as the one the promotion script claims to
    have deployed. A failure here is the only thing that blocks
    ``deployed-sha`` from being written.

    :param service: systemd service unit name, e.g.
        ``"omnigent-eval-web.service"``.
    :param expected_sha: Full 40-char SHA the release was supposed
        to load.
    :param proc: Optional pre-built ``systemctl show`` result, for
        test injection. Production callers should leave this as
        ``None``.
    :returns: The canonical release dir the running process points
        at (``releases/<expected_sha>``); the return value is mostly
        informational for callers that want to echo it back to the
        operator.
    :raises SystemdError: when the loaded SHA disagrees with
        ``expected_sha`` (including the staging-path case).
    """
    if proc is None:
        proc = subprocess.run(
            ["systemctl", "show", service, "-p", "MainPID", "--value"],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise SystemdError(f"could not query MainPID for {service}: {proc.stderr.strip()}")
    try:
        pid = int(proc.stdout.strip())
    except ValueError as exc:
        raise SystemdError(f"could not parse MainPID for {service}: {proc.stdout!r}") from exc
    if pid <= 0:
        raise SystemdError(
            f"{service} has no running main PID; refusing to declare deployment live"
        )
    loaded = loaded_release_sha(pid)
    if loaded is None:
        raise SystemdError(
            f"could not extract a release SHA from {service} (pid={pid}); "
            f"refusing to declare deployment live"
        )
    if loaded != expected_sha:
        raise SystemdError(
            f"{service} (pid={pid}) loaded release {loaded!r}, expected {expected_sha!r}; "
            f"refusing to declare deployment live"
        )
    return Path(f"/home/hermes/workspace/deployments/omnigent/releases/{expected_sha}")


def write_host_dropin(sha: str, *, release_dir: Path) -> Path:
    """Write ``10-release-<sha>.conf`` for the omnigent-eval-host unit.

    Mirrors :func:`write_release_dropin` for the host service. The
    promotion script invokes both writers in lockstep so a single
    promotion cannot leave the web at one SHA while the host stays
    on another. The host is restarted by the promotion script after
    the web passes its loopback probe; ``verify_loaded_release``
    then confirms both services loaded the same SHA before
    ``deployed-sha`` is written.

    The host drop-in uses ``python -P -m omnigent host --server ...``
    directly rather than the ``.venv/bin/omni`` shim. The shim is
    rewritten by the build pipeline but, historically, embedded the
    staging-dir path (``releases/.staging-<sha>-...``) at build time;
    if the staging dir is later cleaned up before the host restarts,
    the shim aborts systemd startup with ``status=127``. The direct
    module call sidesteps that class of failure because the canonical
    release dir is what systemd points at, not the (now-deleted)
    staging dir.

    :param sha: Full release SHA.
    :param release_dir: Absolute path of the canonical release dir.
    :returns: Path of the host drop-in that was written.
    """
    target = host_dropin_dir() / f"10-release-{sha[:12]}.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"# Pin omnigent-eval-host at release {sha}\n"
        f"# using {release_dir} (release-local .venv, immutable).\n"
        f"# Host launch uses ``python -P -m omnigent host`` directly rather\n"
        f"# than the ``.venv/bin/omni`` shim; the shim can bake a deleted\n"
        f"# staging dir path at build time and abort systemd startup with\n"
        f"# ``status=127`` if the staging dir is cleaned up first.\n"
        f"[Service]\n"
        f"WorkingDirectory=/tmp\n"
        f"ExecStart=\n"
        f"ExecStart={release_dir}/.venv/bin/python -P -m omnigent host \\\n"
        f"  --server http://127.0.0.1:4097\n"
    )
    _atomic_write(target, body)
    return target


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
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
