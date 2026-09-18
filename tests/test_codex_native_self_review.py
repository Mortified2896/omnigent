from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from omnigent.harnesses.codex_native import self_review as sr


class _FakeCodexClient:
    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.requests: list[tuple[str, dict]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "thread/fork":
            return {
                "result": {
                    "thread": {"id": "thread_review"},
                    "model": "gpt-5.6-sol",
                    "modelProvider": "openai",
                    "reasoningEffort": "high",
                }
            }
        if method == "turn/start":
            return {"result": {"turn": {"id": "turn_review"}}}
        raise AssertionError(method)

    async def iter_events(self) -> AsyncIterator[dict]:
        for event in self.events:
            yield event


def _successful_events(*, include_tool: bool = False) -> list[dict]:
    events: list[dict] = [
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread_review",
                "turnId": "turn_review",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 1200,
                        "cachedInputTokens": 1100,
                        "cacheWriteInputTokens": 25,
                        "outputTokens": 80,
                        "reasoningOutputTokens": 30,
                    }
                },
            },
        }
    ]
    if include_tool:
        events.append(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread_review",
                    "turnId": "turn_review",
                    "item": {
                        "type": "commandExecution",
                        "id": "cmd_1",
                        "command": "git status",
                    },
                },
            }
        )
    events.extend(
        [
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread_review",
                    "turnId": "turn_review",
                    "item": {
                        "type": "agentMessage",
                        "id": "msg_review",
                        "text": (
                            '{"outcome":"success","confidence":0.86,'
                            '"comment":"The requested change and verification completed.",'
                            '"tags":["Tests/verification"],'
                            '"evidence":["Relevant tests passed."]}'
                        ),
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread_review",
                    "turn": {"id": "turn_review", "status": "completed"},
                },
            },
        ]
    )
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("include_tool", [False, True])
async def test_self_review_fails_before_connect_or_fork(monkeypatch, include_tool) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("Unsafe reviewer must not connect or issue any protocol request")

    monkeypatch.setattr(sr, "client_for_transport", forbidden, raising=False)
    result = await sr.run_self_review(
        socket_path="/tmp/app-server.sock",
        parent_thread_id="thread_primary",
        primary_turn_id="turn_primary",
        timeout_seconds=1,
    )
    assert result is None


def test_missing_usage_stays_unknown_not_zero() -> None:
    usage = sr._usage_from_params(
        {
            "threadId": "thread_review",
            "turnId": "turn_review",
            "tokenUsage": {"last": {"inputTokens": 5}},
        }
    )
    assert usage is not None
    assert usage.input_tokens == 5
    assert usage.output_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
