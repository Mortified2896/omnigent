"""Read existing, explicitly configured loopback OmniRoute credentials."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import tomllib


def codex_mcp_omniroute_key(
    base_url: str,
    config_path: Path | None = None,
) -> str | None:
    """Read the existing loopback OmniRoute MCP bearer without persisting it."""
    path = config_path or Path.home() / ".codex" / "config.toml"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        entry = config["mcp_servers"]["omniroute"]
        mcp_url = urlsplit(entry["url"])
        target = urlsplit(base_url)
        if (
            mcp_url.scheme != "http"
            or mcp_url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or target.scheme != "http"
            or target.hostname not in {"127.0.0.1", "localhost", "::1"}
            or mcp_url.port != target.port
        ):
            return None
        headers = entry.get("http_headers", {})
        value = headers.get("Authorization") or headers.get("authorization")
        return value.strip() if isinstance(value, str) and value.strip() else None
    except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
        return None
