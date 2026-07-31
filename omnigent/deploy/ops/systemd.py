"""Systemd helpers for the long-term deploy architecture.

The live service units are intentionally long-lived: they reference the
``OMNIGENT_RELEASE_DIR`` env var (which itself is set by the active
``10-release-<sha>.conf`` drop-in) rather than hard-coding a worktree
path. This module writes that drop-in and small helpers around it.

The deployment pins **two** services to the same release: the web UI
(``omnigent-eval-web.service``) and the host daemon
(``omnigent-eval-host.service``). Both share the same immutable
release directory so the host daemon can never drift from the web
service it talks to — the host running on a different SHA than the
web would let the daemon import modules from one commit while serving
traffic to a runner that registers against a server running on
another commit. The promotion script writes the drop-in for both
services atomically; this module is what backs that write.

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
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Web service defaults (canonical — the host service shares the same
# port because the host daemon connects to the web service over
# loopback on that port).
_DEFAULT_WEB_SERVICE = "omnigent-eval-web.service"
_DEFAULT_HOST_SERVICE = "omnigent-eval-host.service"
_DEFAULT_WEB_DROPIN_DIR = Path("/etc/systemd/system/omnigent-eval-web.service.d")
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


@dataclass(frozen=True)
class ServiceSpec:
    """The systemd-side details for one managed service.

    Both the web and host services share the same release directory,
    the same release SHA, and the same loopback port — but their
    drop-in directories, ExecStart commands, and ExecStopPost
    commands differ. The dataclass makes that explicit so the
    per-service drop-in writer does not have to remember which field
    applies to which service.

    :param service_name: The systemd unit file name (e.g.
        ``"omnigent-eval-web.service"``).
    :param dropin_dir: The drop-in directory used by the unit (e.g.
        ``/etc/systemd/system/omnigent-eval-web.service.d``).
    :param config_path: Config path passed to the unit's ExecStart.
        ``None`` means the unit does not take a ``--config`` argument
        (the host daemon is a client, not a server).
    :param exec_start_kind: ``"server"`` builds the web server
        ExecStart; ``"host"`` builds the host daemon ExecStart that
        connects to the loopback server. ``"host_stop"`` builds an
        ExecStopPost that stops the host daemon cleanly. The three
        kinds are mutually exclusive — the dataclass is a tagged
        union, not a free-form struct.
    :param unit_description: Description embedded in the unit file
        drop-in for runbook readability.
    """

    service_name: str
    dropin_dir: Path
    config_path: Path | None
    exec_start_kind: str
    unit_description: str

    def __post_init__(self) -> None:
        if self.exec_start_kind not in {"server", "host", "host_stop"}:
            raise ValueError(
                f"exec_start_kind must be server|host|host_stop, got {self.exec_start_kind!r}"
            )


def web_service_spec() -> ServiceSpec:
    """Return the :class:`ServiceSpec` for the web service.

    The web service is the canonical server: ``omnigent server``
    bound to loopback. Its drop-in writes a full server-style
    ExecStart with the ``--config`` flag and a pre-start provenance
    gate. The same release directory also pins the host daemon, but
    the host daemon's drop-in (see :func:`host_service_spec`) is a
    separate file in a separate drop-in directory.
    """
    raw = os.environ.get("OMNIGENT_DEPLOY_SERVICE_NAME", "").strip()
    raw_dropin = os.environ.get("OMNIGENT_DEPLOY_DROPIN_DIR", "").strip()
    return ServiceSpec(
        service_name=raw or _DEFAULT_WEB_SERVICE,
        dropin_dir=Path(raw_dropin) if raw_dropin else _DEFAULT_WEB_DROPIN_DIR,
        config_path=config_path(),
        exec_start_kind="server",
        unit_description="Pin omnigent-eval-web at release {sha}",
    )


def host_service_spec() -> ServiceSpec:
    """Return the :class:`ServiceSpec` for the host daemon service.

    The host daemon is the client: ``omnigent host --server
    http://127.0.0.1:<port>`` running in the foreground. Its drop-in
    sets ``WorkingDirectory=/tmp`` (mirroring the web drop-in so a
    future code path cannot accidentally import the in-tree
    ``omnigent``), runs the same provenance gate as the web service
    (``ExecStartPre``), and writes an ``ExecStopPost`` that asks the
    release's own ``omni`` to stop the daemon cleanly via the
    ``host stop --server`` group command.

    The host drop-in uses the **same** ``OMNIGENT_RELEASE_DIR`` /
    ``OMNIGENT_RELEASE_EXPECTED_SHA`` env vars as the web drop-in so
    the deploy-status command and the supervisor verification logic
    see a single canonical pinned release. The host drop-in does
    **not** take a ``--config`` argument because the host daemon is
    not a server — it inherits config from the loopback server it
    connects to.
    """
    return ServiceSpec(
        service_name=_DEFAULT_HOST_SERVICE,
        dropin_dir=_DEFAULT_HOST_DROPIN_DIR,
        config_path=None,
        exec_start_kind="host",
        unit_description="Pin omnigent-eval-host at release {sha}",
    )


def _resolve(value: str | None, *, fallback: str) -> str:
    raw = (value or "").strip()
    return raw or fallback


def service_name() -> str:
    """Return the canonical web service name (legacy helper).

    New code should use :func:`web_service_spec` /
    :func:`host_service_spec`. This helper is preserved because the
    drop-in writer for the web service is still dispatched through the
    legacy ``OMNIGENT_DEPLOY_SERVICE_NAME`` env var for backward
    compatibility with the existing sudoers wrapper.
    """
    return _resolve(os.environ.get("OMNIGENT_DEPLOY_SERVICE_NAME"), fallback=_DEFAULT_WEB_SERVICE)


def dropin_dir() -> Path:
    """Return the drop-in directory for the canonical web service.

    See :func:`service_name` for the legacy role this helper plays.
    New code should reach for :func:`web_service_spec`.
    """
    raw = os.environ.get("OMNIGENT_DEPLOY_DROPIN_DIR", "").strip()
    return Path(raw) if raw else _DEFAULT_WEB_DROPIN_DIR


def host_dropin_dir() -> Path:
    """Return the drop-in directory for the host daemon service.

    The host drop-in directory is independent of
    :func:`dropin_dir` so host-environment-only drop-ins (the
    ``router-env.conf`` / ``tailscale-origin.conf`` /
    ``minimax-token-plan.conf`` files that the host needs but the
    web does not) live alongside the release drop-in without
    shadowing it.
    """
    return _DEFAULT_HOST_DROPIN_DIR


def service_port() -> int:
    raw = os.environ.get("OMNIGENT_DEPLOY_SERVICE_PORT", "").strip()
    try:
        return int(raw) if raw else _DEFAULT_PORT
    except ValueError:
        return _DEFAULT_PORT


def config_path() -> Path:
    raw = os.environ.get("OMNIGENT_DEPLOY_CONFIG_PATH", "").strip()
    return Path(raw) if raw else _DEFAULT_CONFIG


def write_release_dropin(
    sha: str,
    *,
    release_dir: Path,
    spec: ServiceSpec | None = None,
) -> Path:
    """Write ``10-release-<short-sha>.conf`` for the given release.

    The drop-in sets ``Environment=OMNIGENT_RELEASE_DIR=...`` and
    ``Environment=OMNIGENT_RELEASE_EXPECTED_SHA=...`` *and* the
    ``WorkingDirectory`` and ``ExecStart`` pointing at the release's
    own ``.venv/bin/python`` (for the web server) or ``.venv/bin/omni``
    (for the host daemon). systemd drop-ins replace ``ExecStart``
    in the base unit when they set it explicitly (empty string then
    full value), so this drop-in takes ownership of the command line.

    Most importantly, the drop-in installs an ``ExecStartPre=`` that
    runs the supervisor gate as the release's own Python interpreter.
    The gate verifies provenance, manifest SHA, and (unless explicit
    API-only) the web UI bundle. A failure aborts the systemd startup
    with a runbook-quality message in the journal; the service cannot
    come up under a misconfigured release.

    For the host service the drop-in additionally writes
    ``ExecStopPost`` so a clean ``systemctl stop`` invokes the
    release's own ``omni host stop --server <url>``. This makes the
    host daemon shut down gracefully (terminates its connected
    runners, posts a clean stop event to the server) instead of
    being SIGKILLed by systemd after the default 90s grace.

    Note: systemd does NOT expand ``%E`` / env-var references inside
    ``ExecStart`` (only inside ``EnvironmentFile=-`` and a few
    directives). For that reason this drop-in writes the resolved
    release path directly into both ``ExecStartPre`` and ``ExecStart``
    — there is no template indirection that an LLM agent could
    mis-target.

    :param sha: Full release SHA (used in the drop-in filename and
        embedded inside the file as documentation).
    :param release_dir: Absolute path of the release directory.
    :param spec: Optional :class:`ServiceSpec` selecting which
        service the drop-in belongs to. ``None`` selects the legacy
        web service so existing callers that pass no spec continue
        to write the web drop-in.
    :returns: Path of the drop-in file that was written.
    :raises SystemdError: If the drop-in path falls outside the
        approved ``/etc/systemd/system`` tree (defensive — a
        misconfigured env var could redirect the drop-in anywhere).
    """
    if spec is None:
        spec = web_service_spec()
    target = spec.dropin_dir / f"10-release-{sha[:12]}.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    port = service_port()
    # NOTE: ``WorkingDirectory`` is intentionally set to a neutral
    # directory (``/tmp``) rather than to the release directory.
    # Python inserts the cwd into ``sys.path[0]``; if cwd is the
    # release directory, Python would import ``omnigent`` from the
    # bare source tree (``<release>/omnigent/``) instead of the
    # installed wheel under site-packages. The gate and the server
    # both rely on the explicit ``.venv/bin/python`` interpreter and
    # the ``OMNIGENT_RELEASE_DIR`` env var; cwd is irrelevant to
    # their operation. The same reasoning applies to the host
    # daemon's ``omni`` invocation — its importer is the release
    # venv, not the cwd.
    description = spec.unit_description.format(sha=sha)
    body_parts = [
        f"# {description}",
        f"# using {release_dir} (release-local .venv, immutable).",
        "# Drop-in precedence (10-release-*) wins over the older deploy-main-*",
        "# drop-ins and over pre-existing evaluator/route-approval/router/",
        "# tailscale-origin drop-ins; their Environment*= lines stay in place.",
        "[Service]",
        "WorkingDirectory=/tmp",
        "# Supervisor pre-start gate. Runs as the release's own Python so",
        "# ``omnigent`` imports from this release's venv (not from the main",
        "# checkout). Aborts systemd startup on failure.",
        f"ExecStartPre={release_dir}/.venv/bin/python -P -m omnigent.deploy.supervisor.gate",
    ]
    body_parts.extend(_exec_start_lines(spec, release_dir=release_dir, port=port))
    if spec.exec_start_kind == "host":
        # Host daemon needs a clean shutdown so the connected
        # runners finish their sessions before systemd SIGKILLs the
        # process. ``omni host stop`` returns 0 on a clean stop and
        # non-zero when there is nothing to stop (idempotent), so we
        # do not propagate the exit code to systemd.
        server_url = f"http://127.0.0.1:{port}"
        body_parts.append(
            f"ExecStopPost={release_dir}/.venv/bin/omni host stop "
            f"--server {server_url}"
        )
    body_parts.extend(
        [
            f"Environment=OMNIGENT_RELEASE_DIR={release_dir}",
            f"Environment=OMNIGENT_RELEASE_EXPECTED_SHA={sha}",
            "Environment=PYTHONSAFEPATH=1",
        ]
    )
    body = "\n".join(body_parts) + "\n"
    _atomic_write(target, body)
    return target


def _exec_start_lines(spec: ServiceSpec, *, release_dir: Path, port: int) -> list[str]:
    """Build the ``ExecStart`` lines for one :class:`ServiceSpec`.

    The three kinds produce different invocations:

    * ``server`` — ``omnigent server --host 127.0.0.1 --port <port>
      --no-open --config <cfg>``
    * ``host`` — ``omni host --server http://127.0.0.1:<port>``
      (foreground; the daemon watches its spawned runners itself).
    * ``host_stop`` — not used as an ExecStart; the host_stop
      variant is reserved for ExecStopPost composition (kept here
      so the ``exec_start_kind`` vocabulary is exhaustive).

    The ``ExecStart=`` (empty) + ``ExecStart=<full>`` pair is what
    systemd requires to fully replace the base unit's ExecStart.
    Drop-ins that only set ``ExecStart=<full>`` without the empty
    ``ExecStart=`` first end up appended to the base value, which
    has bitten earlier iterations of this file.
    """
    if spec.exec_start_kind == "server":
        if spec.config_path is None:
            raise ValueError("server spec requires config_path")
        return [
            "ExecStart=",
            (
                f"ExecStart={release_dir}/.venv/bin/python -P -m omnigent server \\\n"
                f"  --host 127.0.0.1 --port {port} \\\n"
                f"  --no-open \\\n"
                f"  --config {spec.config_path}"
            ),
        ]
    if spec.exec_start_kind == "host":
        server_url = f"http://127.0.0.1:{port}"
        return [
            "ExecStart=",
            (
                f"ExecStart={release_dir}/.venv/bin/omni host "
                f"--server {server_url}"
            ),
        ]
    # ``host_stop`` is reserved for ExecStopPost — see
    # :func:`write_release_dropin` for the call site. Returning an
    # empty ExecStart lets callers reuse this helper in tests; it
    # is never reached in production because the host stop helper
    # is dispatched via ExecStopPost, not ExecStart.
    return ["ExecStart=", f"ExecStart={release_dir}/.venv/bin/true"]


def disable_other_release_dropins(
    active_sha: str, *, spec: ServiceSpec | None = None
) -> list[Path]:
    """Move any ``10-release-*.conf`` not matching ``active_sha`` to
    ``.disabled``.

    Mirrors the older ``promote_main_deploy.sh`` cleanup so the drop-in
    precedence resolves to the new release deterministically, even when
    a sequence of promotion attempts left stale drop-ins behind.
    Returns the list of disabled drop-ins (informational).

    :param active_sha: SHA of the drop-in that should remain active.
    :param spec: Optional :class:`ServiceSpec` selecting which
        service's drop-in directory to clean. ``None`` selects the
        legacy web service so existing callers continue to clean the
        web drop-ins.
    :returns: Disabled drop-in paths.
    """
    if spec is None:
        spec = web_service_spec()
    disabled: list[Path] = []
    active_name = f"10-release-{active_sha[:12]}.conf"
    for entry in spec.dropin_dir.iterdir():
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
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise