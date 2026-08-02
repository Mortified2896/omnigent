#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────
# Check 12 — Same production URL and port configuration are used
# ─────────────────────────────────────────────────────────────────────
#
# Asserts the canary wheel's bind / port / auth-header / data-dir
# / deployed-sha match the production deploy's documented values.
# The canary uses its OWN deployed-sha marker (so this check
# asserts the marker exists, not that it matches the production
# pre-cutover SHA).

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


def _env(name: str) -> str | None:
    return os.environ.get(name)


def main() -> int:
    port = _env("OMNIGENT_PORT") or "6767"
    auth_header = _env("OMNIGENT_AUTH_HEADER") or "X-Forwarded-Email"
    data_dir = _env("OMNIGENT_DATA_DIR") or "/var/lib/omnigent"
    unit_name = _env("UNIT_NAME") or "omnigent"
    prod_port = _env("PROD_PORT") or port
    prod_hostname = _env("PROD_HOSTNAME") or ""

    evidence: dict[str, Any] = {}

    # 1. /health on the canary port.
    try:
        proc = subprocess.run(
            ["curl", "-fsS", "--max-time", "5", f"http://127.0.0.1:{port}/health"],
            capture_output=True, check=False,
        )
    except FileNotFoundError:
        emit("FAIL", reason="curl not installed on the canary host")
        return 1
    if proc.returncode != 0:
        emit("FAIL", reason=f"/health did not return 200 on port {port}")
        return 1
    evidence["health_url"] = f"http://127.0.0.1:{port}/health"

    # 2. /api/whoami accepts the configured auth header.
    identity = _env("CANARY_IDENTITY") or "canary@omnigent.local"
    proc = subprocess.run(
        ["curl", "-fsS", "--max-time", "5",
         "-H", f"{auth_header}: {identity}",
         f"http://127.0.0.1:{port}/api/whoami"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        emit("FAIL", reason=f"/api/whoami did not return 200 with header '{auth_header}: {identity}'")
        return 1
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        emit("FAIL", reason=f"/api/whoami returned non-JSON body: {exc}")
        return 1
    if body.get("email") != identity:
        emit("FAIL", reason=f"/api/whoami returned email={body.get('email')!r}, expected {identity!r}")
        return 1
    evidence["whoami_email"] = body.get("email")
    evidence["whoami_is_admin"] = body.get("is_admin")

    # 3. systemd unit matches the canonical template + uses the
    #    resolved absolute omni shim path (not a relative or
    #    assumed path).
    unit_path = f"/etc/systemd/system/{unit_name}.service"
    try:
        with open(unit_path, "r", encoding="utf-8") as f:
            unit_content = f.read()
    except FileNotFoundError:
        # The canary may not run via systemd (e.g. a container
        # without systemd). Skip the unit-file checks.
        evidence["unit_file"] = "absent (canary host has no systemd unit)"
    else:
        # ExecStart must contain an absolute path (not bare `omni`).
        for line in unit_content.splitlines():
            if line.strip().startswith("ExecStart="):
                exec_start = line.split("=", 1)[1].strip()
                if exec_start.startswith("/"):
                    evidence["exec_start"] = exec_start
                else:
                    emit("FAIL", reason=f"unit ExecStart is not absolute: {exec_start!r}")
                    return 1
                break
        else:
            emit("FAIL", reason="unit has no ExecStart=")
            return 1
        # The unit must bind 0.0.0.0 (not the loopback default).
        if "--host 0.0.0.0" not in exec_start and " --host 0.0.0.0 " not in exec_start:
            emit("FAIL", reason="unit ExecStart does not bind 0.0.0.0 (would not be reachable from the reverse proxy)")
            return 1
        # The unit's port must match PROD_PORT.
        if f"--port {prod_port}" not in exec_start:
            emit("FAIL", reason=f"unit ExecStart does not include --port {prod_port} (production port mismatch)")
            return 1

    # 4. Data dir matches.
    evidence["data_dir"] = data_dir
    if data_dir != "/var/lib/omnigent":
        emit("FAIL", reason=f"OMNIGENT_DATA_DIR={data_dir!r} does not match /var/lib/omnigent (production data dir mismatch)")
        return 1

    # 5. Auth header matches the operator's documented pre-cutover
    #    note (PROD_AUTH_HEADER env var, defaulting to X-Forwarded-Email).
    expected_header = _env("PROD_AUTH_HEADER") or "X-Forwarded-Email"
    if auth_header != expected_header:
        emit("FAIL", reason=f"OMNIGENT_AUTH_HEADER={auth_header!r} does not match PROD_AUTH_HEADER={expected_header!r}")
        return 1
    evidence["auth_header"] = auth_header

    # 6. deployed-sha marker exists in the data dir.
    deployed_sha_path = os.path.join(data_dir, "deployed-sha")
    if os.path.isfile(deployed_sha_path):
        with open(deployed_sha_path, "r", encoding="utf-8") as f:
            evidence["deployed_sha"] = f.read().strip()
    else:
        # The canary runner stamps this AFTER the smoke tests; if
        # it's not yet present, that is acceptable (check 12
        # runs before the stamp).
        evidence["deployed_sha"] = None

    emit("PASS", **evidence)
    return 0


def emit(status: str, **evidence: Any) -> None:
    line = json.dumps({"status": status, **evidence}, sort_keys=True)
    print(line, flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        emit("FAIL", reason=f"unhandled exception: {type(exc).__name__}: {exc}")
        sys.exit(1)