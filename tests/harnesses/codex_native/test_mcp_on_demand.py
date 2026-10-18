"""Specialist MCPs remain available without booting in every native chat."""

from __future__ import annotations

import asyncio
import copy
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import tomlkit

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native import mcp_on_demand as mcp


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(mcp.STARTUP_SERVERS_ENV, "omniroute")
    monkeypatch.setattr(mcp, "_reload_servers", AsyncMock())
    (tmp_path / "codex-home").mkdir()
    app_server._inject_mcp_server_config(
        tmp_path / "codex-home",
        tmp_path,
        "/python",
        mcp_servers={
            "omniroute": {"url": "http://localhost:20128/mcp"},
            "anki": {"command": "anki", "env": {"SECRET": "private-value"}},
            "unity-mcp": {"command": "unity", "tools": {"read": {"approval_mode": "prompt"}}},
            "unity_mcp": {"command": "unity"},
            "required": {"command": "required", "required": True},
            "off": {"command": "off", "enabled": False},
        },
    )
    return tmp_path


def _config(path: Path) -> dict:
    return tomlkit.parse((path / "codex-home/config.toml").read_text()).unwrap()["mcp_servers"]


def test_startup_defers_only_optional_servers(session: Path) -> None:
    config = _config(session)
    assert {name for name, value in config.items() if value.get("enabled") is not False} == {
        "omniroute",
        "required",
        "omnigent",
    }
    assert config["anki"]["env"] == {"SECRET": "private-value"}
    state = json.loads((session / "mcp_on_demand.json").read_text())
    assert "off" not in state["available"]
    assert "private-value" not in json.dumps(state)
    assert stat.S_IMODE((session / "mcp_on_demand.json").stat().st_mode) == 0o600


def test_default_hosts_and_source_inventory_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(mcp.STARTUP_SERVERS_ENV, raising=False)
    servers = {"anki": {"command": "anki"}}
    original = copy.deepcopy(servers)
    assert mcp.prepare_on_demand_servers(servers, tmp_path) == original
    assert servers == original
    assert mcp.activation_tool_schema(tmp_path) is None


def test_relay_advertises_and_runs_activation_without_a_turn(session: Path) -> None:
    schema = bridge._handle_mcp_request("tools/list", {}, {}, session)["tools"]
    activation = next(tool for tool in schema if tool["name"] == mcp.TOOL_NAME)
    assert "anki" in activation["description"]
    assert "private-value" not in json.dumps(schema)
    result = bridge._call_mcp_tool(
        {"name": mcp.TOOL_NAME, "arguments": {"action": "enable", "name": "anki"}},
        {},
        session,
    )
    assert not result.get("isError")
    assert _config(session)["anki"]["enabled"] is True
    mcp._reload_servers.assert_awaited_once_with(session)
    assert "private-value" not in json.dumps(result)
    mcp.call_activation_tool(session, {"action": "disable", "name": "anki"})
    assert _config(session)["anki"]["enabled"] is False


@pytest.mark.parametrize("name", ["off", "unknown", "omnigent", "required", "omniroute"])
def test_cannot_enable_disabled_unknown_or_change_core_servers(session: Path, name: str) -> None:
    before = _config(session)
    result = mcp.call_activation_tool(session, {"action": "enable", "name": name})
    assert result["isError"] is True
    assert _config(session) == before
    mcp._reload_servers.assert_not_awaited()


def test_enabled_specialist_survives_resume_and_source_removal(session: Path) -> None:
    mcp.call_activation_tool(session, {"action": "enable", "name": "anki"})
    source = {"anki": {"command": "anki"}, "other": {"command": "other"}}
    result = mcp.prepare_on_demand_servers(source, session)
    assert result["anki"]["enabled"] is True
    assert result["other"]["enabled"] is False
    assert source == {"anki": {"command": "anki"}, "other": {"command": "other"}}
    result = mcp.prepare_on_demand_servers({"other": {"command": "other"}}, session)
    assert "anki" not in json.loads((session / "mcp_on_demand.json").read_text())["available"]


def test_duplicate_unity_alias_cannot_start_second_transport(session: Path) -> None:
    assert not mcp.call_activation_tool(session, {"action": "enable", "name": "unity-mcp"}).get(
        "isError"
    )
    result = mcp.call_activation_tool(session, {"action": "enable", "name": "unity_mcp"})
    assert result["isError"] is True
    assert "already active as unity-mcp" in result["content"][0]["text"]
    assert _config(session)["unity_mcp"]["enabled"] is False


def test_reload_failure_restores_config_and_does_not_leak_credentials(
    session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (session / "codex-home/config.toml").read_bytes()
    state_before = (session / "mcp_on_demand.json").read_bytes()
    monkeypatch.setattr(
        mcp, "_reload_servers", AsyncMock(side_effect=RuntimeError("private-value"))
    )
    result = mcp.call_activation_tool(session, {"action": "enable", "name": "anki"})
    assert result["isError"] is True
    assert "private-value" not in json.dumps(result)
    assert (session / "codex-home/config.toml").read_bytes() == before
    assert (session / "mcp_on_demand.json").read_bytes() == state_before


def test_separate_chat_activation_is_isolated(session: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    mcp.prepare_on_demand_servers({"anki": {"command": "anki"}}, other)
    mcp.call_activation_tool(session, {"action": "enable", "name": "anki"})
    assert json.loads((other / "mcp_on_demand.json").read_text())["active"] == []


def test_corrupt_activation_metadata_does_not_break_core_tool_discovery(session: Path) -> None:
    (session / "mcp_on_demand.json").write_text("{broken")
    assert bridge._handle_mcp_request("tools/list", {}, {}, session) == {"tools": []}
    assert mcp.call_activation_tool(session, {"action": "list"})["isError"] is True


@pytest.mark.parametrize("setting", [None, "", "omniroute"])
@pytest.mark.parametrize("server_url", [None, "http://127.0.0.1:6767"])
def test_startup_setting_survives_daemon_and_runner_boundaries(
    monkeypatch: pytest.MonkeyPatch, setting: str | None, server_url: str | None
) -> None:
    from omnigent.cli import _build_host_daemon_env
    from omnigent.host.connect import _build_runner_env

    monkeypatch.delenv(mcp.STARTUP_SERVERS_ENV, raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    if setting is not None:
        monkeypatch.setenv(mcp.STARTUP_SERVERS_ENV, setting)
    daemon_env = _build_host_daemon_env(server_url=server_url)
    runner_env = _build_runner_env(
        daemon_env,
        server_url=server_url or "http://localhost:8000",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )
    for env in (daemon_env, runner_env):
        assert env.get(mcp.STARTUP_SERVERS_ENV) == setting


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
def test_activation_is_governed_by_native_policy_hook(event: str) -> None:
    from omnigent.native.native_policy_hook import hook_payload_to_evaluation_request

    arguments = {"action": "enable", "name": "anki"}
    request = hook_payload_to_evaluation_request(
        event,
        {"tool_name": "mcp__omnigent__sys_mcp_servers", "tool_input": arguments},
    )
    assert request is not None
    data = request["event"]["data" if event == "PreToolUse" else "request_data"]
    assert data == {"name": "mcp__omnigent__sys_mcp_servers", "arguments": arguments}


def test_list_reports_native_readiness_without_exposing_transport_config(
    session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp.call_activation_tool(session, {"action": "enable", "name": "anki"})
    status = AsyncMock(return_value={"anki": {"runtime_status": "connected", "tools": ["stats"]}})
    monkeypatch.setattr(mcp, "_runtime_status", status)
    result = mcp.call_activation_tool(session, {"action": "list", "name": "anki"})
    status.assert_awaited_once_with(session, {"anki"})
    payload = json.loads(result["content"][0]["text"])
    anki = next(server for server in payload["servers"] if server["name"] == "anki")
    assert anki["runtime_status"] == "connected" and anki["tools"] == ["stats"]
    assert "private-value" not in json.dumps(result)


@pytest.mark.parametrize("final_status", ["connected", "failed"])
def test_runtime_readiness_waits_for_slow_start_and_hides_transport_errors(
    session: Path, monkeypatch: pytest.MonkeyPatch, final_status: str
) -> None:
    from omnigent.harnesses.codex_native import bridge as codex_bridge

    native = codex_bridge.CodexNativeBridgeState(
        session_id="test",
        socket_path="ws://localhost:1234",
        thread_id="thread",
        codex_home="/home",
    )
    monkeypatch.setattr(codex_bridge, "read_bridge_state", lambda path: native)
    statuses = ["starting", final_status]
    client = SimpleNamespace(
        connect=AsyncMock(),
        close=AsyncMock(),
        request=AsyncMock(
            side_effect=[
                {
                    "result": {
                        "data": [
                            {
                                "name": "anki",
                                "runtimeStatus": status,
                                "tools": {},
                                "toolsError": "private-value",
                            }
                        ]
                    }
                }
                for status in statuses
            ]
        ),
    )
    monkeypatch.setattr(app_server, "CodexAppServerClient", lambda **kwargs: client)
    result = asyncio.run(mcp._runtime_status(session, {"anki"}))
    assert result == {"anki": {"runtime_status": final_status, "tools": []}}
    assert client.request.await_count == 2
    client.close.assert_awaited_once()
