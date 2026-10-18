"""Session-local activation of user MCPs through Codex's supported reload API."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any

import tomlkit
from filelock import FileLock
from tomlkit.exceptions import TOMLKitError
from websockets.exceptions import WebSocketException

from omnigent.harnesses.codex_native.launch_args import _write_private_config

STARTUP_SERVERS_ENV = "OMNIGENT_CODEX_MCP_STARTUP_SERVERS"
TOOL_NAME = "sys_mcp_servers"
_STATE_FILE = "mcp_on_demand.json"


class ActivationError(ValueError):
    """A credential-free message safe to expose to the model."""


def _read_state(bridge_dir: Path) -> dict[str, Any] | None:
    try:
        state = json.loads((bridge_dir / _STATE_FILE).read_text())
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or not all(
        isinstance(state.get(key), list) and all(isinstance(name, str) for name in state[key])
        for key in ("available", "active", "always")
    ):
        raise ActivationError("Invalid session MCP activation state")
    return state


def prepare_on_demand_servers(servers: dict[str, Any], bridge_dir: Path) -> dict[str, Any]:
    """Defer optional ambient MCPs only when the host explicitly opts in.

    Required MCPs and configured startup names stay enabled. Explicitly disabled
    user MCPs stay disabled and cannot be enabled by the activation tool. Only
    names are persisted here; credentials remain in Codex's private config.
    """
    setting = os.environ.get(STARTUP_SERVERS_ENV)
    if setting is None:
        (bridge_dir / _STATE_FILE).unlink(missing_ok=True)
        return servers
    startup = {name.strip() for name in setting.split(",") if name.strip()}
    result = copy.deepcopy(servers)
    with FileLock(str(bridge_dir / "mcp_on_demand.lock")):
        previous = _read_state(bridge_dir)
        available = {
            name
            for name, config in result.items()
            if name != "omnigent" and config.get("enabled") is not False
        }
        always = {
            name for name in available if name in startup or result[name].get("required") is True
        }
        active = always | (set(previous["active"]) & available if previous else set())
        for name in available:
            result[name]["enabled"] = name in active
        _write_private_config(
            bridge_dir / _STATE_FILE,
            json.dumps(
                {
                    "available": sorted(available),
                    "active": sorted(active),
                    "always": sorted(always),
                }
            ),
        )
    return result


def activation_tool_schema(bridge_dir: Path) -> dict[str, Any] | None:
    """Expose a small discovery tool even while specialist servers are stopped."""
    try:
        state = _read_state(bridge_dir)
    except (OSError, ValueError):
        # Bad optional metadata must not break the core relay's tool discovery.
        return None
    if state is None:
        return None
    specialists = sorted(set(state["available"]) - set(state["always"]))
    if not specialists:
        return None
    return {
        "name": TOOL_NAME,
        "description": (
            "Manage specialist MCP servers for this chat only. They are stopped until needed. "
            "Before a task needs an integration, enable its server here; its native MCP tools "
            "become available on the next step. Use list to inspect activation. Disable when "
            "finished if useful. This preserves the user's global Codex configuration. "
            "Available specialists: " + ", ".join(specialists) + "."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "enable", "disable"]},
                "name": {"type": "string", "description": "Exact configured MCP server name."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }


async def _reload_servers(bridge_dir: Path) -> None:
    # Lazy imports keep the ordinary relay's startup path unchanged.
    from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
    from omnigent.harnesses.codex_native.bridge import read_bridge_state

    state = read_bridge_state(bridge_dir)
    if state is None:
        raise RuntimeError("The native Codex chat is not running")
    address = state.socket_path
    client = (
        CodexAppServerClient(ws_url=address, client_name="omnigent-mcp-activation")
        if address.startswith(("ws://", "wss://"))
        else CodexAppServerClient(socket_path=Path(address), client_name="omnigent-mcp-activation")
    )
    try:
        async with asyncio.timeout(15):
            await client.connect()
            await client.request("config/mcpServer/reload", {})
    finally:
        await client.close()


def call_activation_tool(bridge_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Change an allowlisted enabled flag, then queue a native MCP refresh.

    Serialize concurrent calls and restore both files on reload failure. Never
    accept commands, URLs, credentials, config paths, or another session ID.
    """
    try:
        with FileLock(str(bridge_dir / "mcp_on_demand.lock"), timeout=20):
            state = _read_state(bridge_dir)
            if state is None:
                raise ActivationError("On-demand MCP activation is not configured for this chat")
            action, name = arguments.get("action"), arguments.get("name")
            if action not in {"list", "enable", "disable"}:
                raise ActivationError("action must be list, enable, or disable")
            if action != "list":
                if not isinstance(name, str) or name not in state["available"]:
                    raise ActivationError("Choose an available server from the list action")
                if name in state["always"]:
                    raise ActivationError(
                        "Startup and required MCP servers cannot be changed here"
                    )
                config_path = bridge_dir / "codex-home" / "config.toml"
                before = config_path.read_text()
                document = tomlkit.parse(before)
                config = document.get("mcp_servers", {}).get(name)
                if not isinstance(config, dict):
                    raise ActivationError("This MCP server is no longer configured")
                enabled = action == "enable"
                if (config.get("enabled") is not False) != enabled:
                    # An identical transport under another alias must not be
                    # launched twice (e.g. unity-mcp and unity_mcp).
                    if enabled:
                        transport = {
                            k: v for k, v in config.items() if k not in {"enabled", "tools"}
                        }
                        for other in state["active"]:
                            peer = document["mcp_servers"].get(other, {})
                            if {
                                k: v for k, v in peer.items() if k not in {"enabled", "tools"}
                            } == transport:
                                raise ActivationError(f"The same MCP is already active as {other}")
                    previous_state = json.dumps(state)
                    active = set(state["active"])
                    if enabled:
                        active.add(name)
                    else:
                        active.discard(name)
                    state["active"] = sorted(active)
                    config["enabled"] = enabled
                    try:
                        _write_private_config(config_path, tomlkit.dumps(document))
                        _write_private_config(bridge_dir / _STATE_FILE, json.dumps(state))
                        asyncio.run(_reload_servers(bridge_dir))
                    except (OSError, RuntimeError, ValueError, WebSocketException):
                        _write_private_config(config_path, before)
                        _write_private_config(bridge_dir / _STATE_FILE, previous_state)
                        raise ActivationError(
                            "Codex could not reload MCP configuration; its config was restored. "
                            "Retry when the native chat is connected."
                        ) from None
            result = {
                "servers": [
                    {
                        "name": server,
                        "enabled": server in state["active"],
                        "startup": server in state["always"],
                    }
                    for server in state["available"]
                ],
                "note": (
                    "Activation is scoped to this chat and retained when it resumes. "
                    "Changed servers refresh on the next model step. Enabled means requested; "
                    "a server can still fail to connect. Use its native tools after activation."
                ),
            }
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
    except (OSError, ValueError, RuntimeError, TypeError, TOMLKitError) as error:
        # Transport/parser exceptions can include credentials; only our own
        # validation and fixed reload messages are safe to surface.
        message = str(error) if isinstance(error, ActivationError) else "MCP activation failed"
        return {"isError": True, "content": [{"type": "text", "text": message}]}
