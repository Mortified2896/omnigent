"""Context inheritance, isolation and tool denial for current-model advice."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from types import SimpleNamespace

import pytest

from omnigent.harnesses.codex_native import advisor, app_server, hook, side_chat
from omnigent.host.advisor_call import AdvisorCallError


class Client:
    def __init__(
        self, bridge_dir, *, trust="trusted", fork_model="current-checkpoint", hang=False
    ):
        self.calls = []
        self.bridge_dir = bridge_dir
        self.trust = trust
        self.fork_model = fork_model
        self.hang = hang
        self.closed = False

    async def connect(self):
        pass

    async def close(self):
        self.closed = True

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "hooks/list":
            return {
                "result": {
                    "data": [
                        {
                            "cwd": "/repo",
                            "hooks": [
                                {
                                    "command": (
                                        "omnigent.harnesses.codex_native.hook evaluate-policy"
                                    ),
                                    "eventName": "preToolUse",
                                    "enabled": True,
                                    "trustStatus": self.trust,
                                }
                            ],
                        }
                    ]
                }
            }
        if method == "thread/fork":
            return {"result": {"model": self.fork_model, "thread": {"id": "child"}}}
        if method == "turn/start":
            assert advisor.is_advisor_context_hook(self.bridge_dir, {"session_id": "child"})
            return {"result": {"turn": {"id": "advice-turn"}}}
        return {"result": {}}

    async def iter_events(self):
        if self.hang:
            await asyncio.Event().wait()
        yield {
            "method": "turn/completed",
            "params": {"threadId": "parent", "turn": {"id": "main"}},
        }
        yield {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "child",
                "turnId": "advice-turn",
                "tokenUsage": {
                    "last": {"inputTokens": 12000, "cachedInputTokens": 11000, "outputTokens": 50}
                },
            },
        }
        yield {
            "method": "item/completed",
            "params": {
                "threadId": "child",
                "turnId": "advice-turn",
                "item": {
                    "type": "agentMessage",
                    "text": '{"candidate_id":"choice","rationale":"Enough"}',
                },
            },
        }
        yield {
            "method": "turn/completed",
            "params": {
                "threadId": "child",
                "turn": {"id": "advice-turn", "status": "completed"},
            },
        }


@pytest.fixture
def context(tmp_path, monkeypatch):
    state = SimpleNamespace(
        session_id="chat", cwd="/repo", thread_id="parent", socket_path="ws://local"
    )
    monkeypatch.setattr(advisor, "bridge_dir_for_bridge_id", lambda _: tmp_path)
    monkeypatch.setattr(advisor, "read_bridge_state", lambda _: state)
    advisor.write_advisor_context_capability(
        tmp_path, "omnigent.harnesses.codex_native.hook evaluate-policy"
    )
    return tmp_path


async def run(client, monkeypatch, **kwargs):
    monkeypatch.setattr(app_server, "client_for_transport", lambda *_a, **_kw: client)
    return await advisor.generate_context_advisor_selection(
        request={"instructions": "Recommend", "task": "Next task", "candidates": []},
        model="current-checkpoint",
        reasoning_effort="low",
        session_id="chat",
        bridge_id="bridge",
        **kwargs,
    )


async def test_inherits_prefix_updates_only_child_effort_and_reports_observed_cache(
    context, monkeypatch
):
    client = Client(context)
    result = await run(client, monkeypatch)
    assert result.cached_input_tokens == 11000
    assert result.input_tokens == 12000
    fork = next(params for method, params in client.calls if method == "thread/fork")
    assert fork == {
        "threadId": "parent",
        "model": "current-checkpoint",
        "ephemeral": True,
        "excludeTurns": True,
        "threadSource": advisor.ADVISOR_THREAD_SOURCE,
    }
    assert ("thread/settings/update", {"threadId": "child", "effort": "low"}) in client.calls
    start = next(params for method, params in client.calls if method == "turn/start")
    assert set(start) == {
        "threadId",
        "input",
    }  # No rewritten tools, schema, instructions or model.
    assert all(
        params.get("threadId") != "parent"
        for method, params in client.calls
        if method != "thread/fork"
    )
    assert client.closed
    assert not advisor.is_advisor_context_hook(context, {"session_id": "child"})


@pytest.mark.parametrize(
    "trust,fork_model,reason",
    [
        ("untrusted", "current-checkpoint", "tool-blocking hook"),
        ("trusted", "changed-checkpoint", "current model changed"),
    ],
)
async def test_fails_closed_without_trusted_guard_or_if_model_changed(
    context, monkeypatch, trust, fork_model, reason
):
    client = Client(context, trust=trust, fork_model=fork_model)
    with pytest.raises(AdvisorCallError, match=reason):
        await run(client, monkeypatch)
    assert not any(method == "turn/start" for method, _ in client.calls)


async def test_timeout_interrupts_only_child_and_keeps_guard_if_completion_is_uncertain(
    context, monkeypatch
):
    client = Client(context, hang=True)
    with pytest.raises(AdvisorCallError, match="budget"):
        await run(client, monkeypatch, timeout_s=0.03)
    assert ("turn/interrupt", {"threadId": "child", "turnId": "advice-turn"}) in client.calls
    assert advisor.is_advisor_context_hook(context, {"session_id": "child"})
    assert client.closed


@pytest.mark.parametrize("tool", ["exec_command", "mcp__omnigent__some_tool", "spawn_agent"])
def test_guard_denies_all_advisor_tools_without_affecting_parent(
    context, monkeypatch, capsys, tool
):
    marker = advisor._marker(context, "child")
    marker.parent.mkdir(exist_ok=True)
    marker.touch()
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "child",
                    "tool_name": tool,
                }
            )
        ),
    )
    assert hook.main(["evaluate-policy", "--bridge-dir", str(context)]) == 0
    assert (
        json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert not advisor.is_advisor_context_hook(context, {"session_id": "parent"})


def test_advisor_fork_is_not_registered_as_a_user_side_chat():
    assert not side_chat.is_omnigent_side_fork(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": "child",
                    "ephemeral": True,
                    "forkedFromId": "parent",
                    "source": advisor.ADVISOR_THREAD_SOURCE,
                }
            },
        }
    )
