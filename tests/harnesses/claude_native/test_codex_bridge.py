"""Tool continuation, subscription qualification and usage contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native.codex_bridge import Bridge, Turn, history_digest, usage_delta
from omnigent.harnesses.claude_native.codex_subscription import bridge_profile
from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig
from omnigent.model_advisor_provider_policy import LogicalChoice, advisor_input
from omnigent.server.model_advisor_service import build_host_catalog


def test_harness_choices_are_distinct_and_old_identity_is_stable():
    codex = LogicalChoice("openai", "fixture-model", "low")
    claude = LogicalChoice("openai", "fixture-model", "low", "claude-native")
    assert codex.choice_id != claude.choice_id
    assert "execution_harness" not in codex.to_payload()
    assert LogicalChoice.from_payload(claude.to_payload()) == claude
    assert {r["harness"] for r in advisor_input("task", (codex, claude))["candidates"]} == {
        "codex-native",
        "claude-native",
    }


def test_catalog_requires_bridge_entitlement_and_keeps_both_harnesses():
    direct = {
        "id": "fixture-model",
        "accessLane": "codex-direct",
        "supportedReasoningEfforts": ["low"],
    }
    bridge = {
        "id": "fixture-model",
        "accessLane": "claude-codex",
        "supportedReasoningEfforts": ["low"],
        "advisorProvider": "openai",
        "advisorHarness": "claude-native",
        "advisorAccessClass": "chatgpt_plan",
        "advisorCanonicalModel": "fixture-model",
        "advisorEntitlementKey": "chatgpt-plan:bridge:fixture",
        "advisorConnectionId": "codex-claude-bridge:fixture",
    }
    catalog = build_host_catalog([direct, bridge])
    assert len(catalog.logical_choices) == 2
    assert {c.harness for c in catalog.logical_choices} == {"codex-native", "claude-native"}
    for c in catalog.logical_choices:
        route = catalog.routes_by_choice[c.choice_id][0]
        assert type(route).from_payload(route.to_payload()) == route
    bridge.pop("advisorEntitlementKey")
    assert len(build_host_catalog([direct, bridge]).logical_choices) == 1


def test_profile_cannot_relabel_an_unrelated_gateway(monkeypatch):
    from omnigent import inference_config

    cfg = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:1234"},
        api_key_helper="fixture-helper",
        routable_models=("fixture-model",),
    )
    profile = {
        "base_url": cfg.env["ANTHROPIC_BASE_URL"],
        "auth_command": "fixture-helper",
        "models": ["fixture-model"],
    }
    monkeypatch.setattr(
        inference_config, "load_runtime_inference_config", lambda: {"claude_codex_bridge": profile}
    )
    assert bridge_profile(cfg) == profile
    cfg.env["ANTHROPIC_BASE_URL"] = "https://other.example"
    assert bridge_profile(cfg) is None


def test_cumulative_usage_is_differenced_with_cache_excluded_for_anthropic():
    old = {"inputTokens": 100, "outputTokens": 10, "cachedInputTokens": 20}
    total = {
        "inputTokens": 300,
        "outputTokens": 50,
        "cachedInputTokens": 80,
        "cacheWriteInputTokens": 15,
    }
    assert usage_delta(total, old) == {
        "input_tokens": 125,
        "output_tokens": 40,
        "cache_read_input_tokens": 60,
        "cache_creation_input_tokens": 15,
    }


@pytest.mark.asyncio
async def test_tool_result_resumes_the_same_rpc_and_all_usage_is_retained():
    class Ws:
        sent = []

        async def send(self, raw):
            self.sent.append(raw)

    rpc = SimpleNamespace(ws=Ws(), events=asyncio.Queue())
    bridge = Bridge({})
    body = {
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "read the file"}],
        "tools": [],
        "output_config": {"effort": "low"},
    }
    from omnigent.harnesses.claude_native.codex_bridge import digest

    turn = Turn(
        rpc, "fixture-model", history_digest(body["messages"]), digest([]), "low", "fixture-thread"
    )
    await rpc.events.put(
        {
            "method": "item/tool/call",
            "id": 17,
            "params": {"tool": "Read", "arguments": {"file_path": "fixture.txt"}},
        }
    )
    first = await bridge.response(body, turn)
    call = first["content"][0]
    body["messages"] += [
        {"role": "assistant", "content": first["content"]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call["id"], "content": "FILE_RESULT"}
            ],
        },
    ]
    assert await bridge.start(body) is turn
    assert "FILE_RESULT" in rpc.ws.sent[-1]
    assert '"id": 17' in rpc.ws.sent[-1]
    # Tool request counters can arrive after the tool-call boundary. The
    # final response must retain both generations, not just tokenUsage.last.
    await rpc.events.put(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {
                    "total": {"inputTokens": 300, "outputTokens": 50},
                    "last": {"inputTokens": 160, "outputTokens": 10},
                }
            },
        }
    )
    await rpc.events.put({"method": "item/agentMessage/delta", "params": {"delta": "FILE_RESULT"}})
    await rpc.events.put({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})

    async def close():
        pass

    rpc.close = close
    final = await bridge.response(body, turn)
    assert final["usage"]["input_tokens"] == 300
    assert final["usage"]["output_tokens"] == 50
    assert final["content"][0]["text"] == "FILE_RESULT"
    assert bridge.pending == {}


def test_claude_stream_block_splitting_and_cache_annotations_keep_history_identity():
    text = {"type": "text", "text": "Reading"}
    tool = {
        "type": "tool_use",
        "id": "fixture-call",
        "name": "Read",
        "input": {"file_path": "fixture.txt"},
    }
    one = [{"role": "assistant", "content": [text, tool]}]
    split = [
        {"role": "assistant", "content": [text]},
        {"role": "assistant", "content": [{**tool, "cache_control": {"type": "ephemeral"}}]},
    ]
    assert history_digest(one) == history_digest(split)
    assert history_digest(one) != history_digest([{"role": "assistant", "content": [text]}])


@pytest.mark.asyncio
async def test_claude_mcp_tool_names_are_translated_and_restored(monkeypatch):
    requests = []

    async def request(method, params):
        requests.append((method, params))
        return {"thread": {"id": "fixture-thread"}}

    rpc = SimpleNamespace(request=request, events=asyncio.Queue())
    bridge = Bridge({"cwd": "/fixture"})

    async def connect():
        return rpc

    monkeypatch.setattr(bridge, "connect", connect)
    body = {
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "Read the fixture"}],
        "tools": [{"name": "mcp__fixture__Read", "input_schema": {"type": "object"}}],
    }
    turn = await bridge.start(body)
    name = requests[0][1]["dynamicTools"][0]["name"]
    assert name != "mcp__fixture__Read" and not name.startswith("mcp__")
    await rpc.events.put(
        {"method": "item/tool/call", "id": 1, "params": {"tool": name, "arguments": {}}}
    )
    response = await bridge.response(body, turn)
    assert response["content"][0]["name"] == "mcp__fixture__Read"
