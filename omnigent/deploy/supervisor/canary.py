"""Candidate validation: spin up the candidate release on a temporary
loopback port and verify it answers HTTP requests the same way the
live service would.

The promotion script calls this *after* the build phase and *before*
the promotion phase. Failing here is a soft veto: the release stays
immutable (no half-built state leaks), the live service is untouched,
and the operator sees an actionable list of which endpoint failed.

Why a loopback canary instead of just trusting ``ExecStartPre``:

* The pre-start gate proves provenance; it does not prove the server
  actually boots and answers.
* A "passed all the static checks but crashes under request" failure
  mode is rare but real: port already in use, database locked, model
  provider 503 at startup, etc.
* Validating on a temporary port means we do not have to coordinate
  with the live service (which may itself be in the middle of a
  conversation).

The canary starts the candidate on a free localhost port, waits for
it to bind, runs a curated set of HTTP probes, and tears the process
down with a SIGTERM (then SIGKILL after a grace period) on success or
failure. It does not touch the live systemd service.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class CanaryError(RuntimeError):
    """Raised when a canary probe fails or the candidate never binds.

    The message names the failing probe so the promotion script's
    error log shows which endpoint to investigate.
    """


def _pick_free_port() -> int:
    """Bind a socket to an ephemeral port, capture the port, close.

    Slightly racy (the port can in principle be reused by another
    process before the canary binds it), but the canary is the only
    thing binding loopback ports in this protocol — and the bind/close
    dance is exactly what other servers do.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_bind(port: int, timeout_s: float) -> bool:
    """Poll ``127.0.0.1:port`` until it accepts a TCP connection.

    Returns True on connect, False on timeout. The polling resolution
    is 50 ms; the timeout default is 30 s which is far longer than a
    healthy uvicorn startup.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _http_get(url: str, *, timeout: float = 5.0) -> tuple[int, str]:
    """Tiny ``GET`` helper that does not import ``requests`` (the canary
    is invoked from the promotion script which does not want a vendored
    dependency tree to fail in this hot path).
    """
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx that *answered*
        return exc.code, exc.read().decode("utf-8", errors="replace") if hasattr(
            exc, "read"
        ) else ""
    except urllib.error.URLError as exc:
        raise CanaryError(f"GET {url} failed: {exc}") from exc


def _build_command(
    release: Path,
    port: int,
    *,
    skip_web_ui: bool,
    config: Path | None,
) -> list[str]:
    """Build the ExecStart-equivalent command for the canary.

    Mirrors the systemd unit's invocation but on a temporary port and
    using a temp database URI. The canary database URI uses a sqlite
    path inside the release directory's ``canary/`` subdir; the live
    service never opens that path.
    """
    python = release / ".venv" / "bin" / "python"
    cmd = [
        str(python),
        "-m",
        "omnigent",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-open",
    ]
    if config is not None:
        cmd.extend(["--config", str(config)])
    return cmd


def _spawn_canary(
    release: Path,
    port: int,
    *,
    log_path: Path,
    skip_web_ui: bool,
    config: Path | None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Spawn the candidate's server and return the ``Popen`` handle.

    ``log_path`` captures combined stdout/stderr so the promotion script
    can attach the canary log to its own failure diagnostics. The canary
    uses its own temp database URI passed via the environment so it
    cannot collide with the live service.
    """
    release_canary_dir = release / "canary"
    release_canary_dir.mkdir(parents=True, exist_ok=True)
    db_path = release_canary_dir / "canary.db"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("w", encoding="utf-8")
    cmd = _build_command(release, port, skip_web_ui=skip_web_ui, config=config)
    env = {
        **os.environ,
        "OMNIGENT_DATABASE_URI": f"sqlite:///{db_path}",
        "OMNIGENT_SKIP_WEB_UI": "1" if skip_web_ui else "",
        "PYTHONUNBUFFERED": "1",
        "HOME": str(Path.home()),
        **(extra_env or {}),
    }
    return subprocess.Popen(
        cmd,
        cwd=release,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        start_new_session=True,
    )


def _terminate(proc: subprocess.Popen[str], grace_s: float = 5.0) -> None:
    """Best-effort SIGTERM-then-SIGKILL cleanup of a canary process.

    The canary runs in its own process group (``start_new_session``)
    so the SIGTERM does not leak to the live service. A SIGKILL after
    the grace window guarantees no orphaned python processes survive.
    """
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=grace_s)
        except Exception:
            pass


def run_canary(
    release: Path,
    *,
    config: Path | None = None,
    skip_web_ui: bool | None = None,
    log_path: Path | None = None,
    bind_timeout_s: float = 30.0,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Boot the release's server on a free loopback port and probe it.

    :param release: Release directory (must contain ``.venv/bin/python``).
    :param config: Optional path to a YAML config for the candidate.
    :param skip_web_ui: Force API-only mode for the canary. ``None``
        reads ``OMNIGENT_SKIP_WEB_UI`` from the environment.
    :param log_path: Where to write the candidate's stdout/stderr.
        Defaults to ``<release>/canary/canary.log``.
    :param bind_timeout_s: How long to wait for the candidate to bind.
    :param extra_env: Extra environment variables for the canary
        process (used by tests to point the candidate at fixtures).
    :returns: Dict with the resolved port, canary PID, and the probe
        outcomes (``/health``, ``/``, an SPA route).
    :raises CanaryError: On any failed probe or bind timeout.
    """
    if skip_web_ui is None:
        skip_web_ui = os.environ.get("OMNIGENT_SKIP_WEB_UI", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    if log_path is None:
        log_path = release / "canary" / "canary.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    port = _pick_free_port()
    proc = _spawn_canary(
        release,
        port,
        log_path=log_path,
        skip_web_ui=skip_web_ui,
        config=config,
        extra_env=extra_env,
    )
    try:
        if not _wait_for_bind(port, bind_timeout_s):
            raise CanaryError(
                f"canary process exited before binding 127.0.0.1:{port}; see {log_path}"
            )
        # `/health` is the universal liveness probe (also used by the
        # public health check script).
        health_status, health_body = _http_get(f"http://127.0.0.1:{port}/health")
        if health_status != 200:
            raise CanaryError(f"/health returned {health_status}: {health_body[:200]!r}")
        root_status, root_body = _http_get(f"http://127.0.0.1:{port}/")
        if root_status != 200:
            raise CanaryError(f"/ returned {root_status}")
        # SPA fallback probe — a SPA refresh on a UI deployment must
        # return the SPA shell, not a 404. We hit a known route like
        # ``/c/<id>`` and require the body to contain the SPA HTML
        # marker. Skipped when ``skip_web_ui`` is set.
        spa_status: int | None = None
        if not skip_web_ui:
            spa_status, spa_body = _http_get(f"http://127.0.0.1:{port}/c/canary")
            if "<!doctype html>" not in spa_body.lower() and "<html" not in spa_body.lower():
                raise CanaryError(
                    f"SPA fallback returned {spa_status} but body is not HTML; "
                    f"first 200 bytes: {spa_body[:200]!r}"
                )
            # Asset reachability — ensure the index.html referenced
            # assets actually resolve on the candidate.
            for asset_rel in _extract_index_assets(root_body):
                asset_url = f"http://127.0.0.1:{port}/{asset_rel.lstrip('/')}"
                asset_status, _ = _http_get(asset_url)
                if asset_status != 200:
                    raise CanaryError(f"referenced asset {asset_rel!r} returned {asset_status}")
        return {
            "port": str(port),
            "pid": str(proc.pid),
            "health": str(health_status),
            "root": str(root_status),
            "spa_fallback": "" if spa_status is None else str(spa_status),
            "log": str(log_path),
        }
    finally:
        _terminate(proc)


def _extract_index_assets(html: str) -> list[str]:
    """Pull the relative asset paths out of an ``index.html`` for the
    reachability probe.

    Limited to ``<script src="...">`` and ``<link rel="stylesheet"
    href="...">``; not exhaustive, but covers the boot-critical
    chunks the failure mode of "build skipped the asset rewrite"
    would show. The first 20 assets are checked, which is enough to
    catch a missing ``index-*.js`` or root css.
    """
    import re

    assets: list[str] = []
    for pattern in (
        re.compile(r'<script[^>]+src="([^"]+)"', re.IGNORECASE),
        re.compile(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', re.IGNORECASE),
    ):
        for match in pattern.finditer(html):
            assets.append(match.group(1))
    return assets[:20]


def main() -> int:
    """CLI entry point: ``python -m omnigent.deploy.supervisor.canary
    <release-dir> [config]``.

    Used by ``scripts/promote_release.sh`` in the candidate-validation
    phase. Returns 0 on success, 1 on failure.
    """
    args = list(sys.argv[1:])
    if not args or len(args) > 2:
        print(f"usage: {sys.argv[0]} <release-dir> [config]", file=sys.stderr)
        return 2
    release = Path(args[0]).resolve()
    config = Path(args[1]).resolve() if len(args) == 2 else None
    try:
        result = run_canary(release, config=config, log_path=release / "canary" / "canary.log")
    except CanaryError as exc:
        print(f"[canary] ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
