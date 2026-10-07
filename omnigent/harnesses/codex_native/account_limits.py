"""Read only subscription windows from the host's signed-in Codex account."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    _allocate_loopback_port,
    _clean_codex_env,
    _find_codex_cli,
    _start_codex_model_discovery_process,
    _stop_codex_model_discovery_process,
    _wait_for_discovery_listener,
)

_cache: tuple[float, dict[str, Any]] | None = None
_lock = asyncio.Lock()


def subscription_windows(result: dict[str, Any]) -> dict[str, Any]:
    buckets = result.get("rateLimitsByLimitId") or {}
    bucket = buckets.get("codex") if isinstance(buckets, dict) else None
    bucket = bucket or result.get("rateLimits") or {}
    windows = []
    for name in ("primary", "secondary"):
        row = bucket.get(name) if isinstance(bucket, dict) else None
        if not isinstance(row, dict):
            continue
        used = row.get("usedPercent")
        if isinstance(used, (float, int)) and not isinstance(used, bool) and 0 <= used <= 100:
            windows.append(
                {
                    "name": name,
                    "remaining_percent": round(100 - used),
                    "window_minutes": row.get("windowDurationMins"),
                    "resets_at": row.get("resetsAt"),
                }
            )
    return {
        "remaining_percent": min((row["remaining_percent"] for row in windows), default=None),
        "windows": windows,
    }


async def read_account_limits() -> dict[str, Any]:
    global _cache
    async with _lock:
        if _cache and time.monotonic() - _cache[0] < 60:
            return _cache[1]
        binary = _find_codex_cli()
        if not binary:
            raise RuntimeError("Codex is not installed")
        # A private config avoids starting the user's MCP servers or applying a
        # gateway provider override. Only the account credential is copied; no
        # credential fields cross the host tunnel or enter a response.
        # Deployments with isolated runner homes can explicitly select the
        # signed-in host account for this read-only probe. Never guess another
        # user's home or change the runner's CODEX_HOME.
        account_home = Path(
            os.environ.get("OMNIGENT_CODEX_ACCOUNT_HOME")
            or os.environ.get("CODEX_HOME")
            or str(Path.home() / ".codex")
        )
        with tempfile.TemporaryDirectory(prefix="omnigent-codex-quota-") as raw:
            root = Path(raw)
            auth = account_home / "auth.json"
            if not auth.is_file():
                raise RuntimeError("No local Codex subscription credentials")
            shutil.copyfile(auth, root / "auth.json")
            (root / "auth.json").chmod(0o600)
            env = _clean_codex_env()
            for key in tuple(env):
                if key.startswith(("OPENAI_", "DATABRICKS_")):
                    env.pop(key, None)
            env["CODEX_HOME"] = str(root)
            port = _allocate_loopback_port()
            url = f"ws://127.0.0.1:{port}"
            process = await _start_codex_model_discovery_process(
                codex_path=binary, listen_url=url, env=env, cwd=root
            )
            client = None
            try:
                await _wait_for_discovery_listener(process, port)
                client = CodexAppServerClient(
                    ws_url=url, client_name="omnigent-subscription-usage"
                )
                await client.connect()
                response = await asyncio.wait_for(
                    client.request("account/rateLimits/read", {}), timeout=15
                )
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Invalid account usage response")
                data = subscription_windows(result)
                _cache = (time.monotonic(), data)
                return data
            finally:
                if client:
                    with contextlib.suppress(Exception):
                        await client.close()
                await _stop_codex_model_discovery_process(process)
