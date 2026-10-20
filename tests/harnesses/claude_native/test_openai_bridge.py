"""Gateway failures preserve the prompt, route warning, tools and cancellation."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from omnigent.harnesses.claude_native.openai_bridge import (
    DIRECT_NOTICE,
    anthropic_request,
    create_app,
    direct_history,
)


class Native:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending = {}
        self.received = []
        self.discarded = []

    async def authorized(self, request):
        return request.headers.get("authorization") == "Bearer test-client"

    async def identity(self, request):
        from starlette.responses import JSONResponse

        return JSONResponse({"account_type": "chatgpt"})

    async def start(self, body):
        self.received.append(body)
        return SimpleNamespace(body=body)

    async def response(self, body, turn):
        return {
            "content": [{"type": "text", "text": "DIRECT ANSWER"}],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }

    async def discard(self, turn):
        self.discarded.append(turn)


@pytest.fixture
def setup(tmp_path):
    key = tmp_path / "key"
    key.write_text("test-gateway")
    profile = {
        "id": "test-model-high",
        "model": "test-model",
        "effort": "high",
        "supported_efforts": ["low", "high"],
    }
    config = {
        "model_profiles": [profile],
        "primary_key_file": str(key),
        "primary_base_url": "http://gateway.invalid/v1",
    }
    native = Native()
    return config, profile, native


def body(stream=False):
    return {
        "model": "test-model-high",
        "stream": stream,
        "messages": [
            {"role": "system", "content": "Keep the constraint"},
            {"role": "user", "content": "Original prompt"},
        ],
    }


def completion(text="GATEWAY ANSWER"):
    return {
        "id": "fixture",
        "model": "upstream-model",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


async def request(app, payload, key="test-client"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as c:
        return await c.post(
            "/v1/chat/completions", json=payload, headers={"Authorization": "Bearer " + key}
        )


@pytest.mark.asyncio
async def test_primary_preserves_messages_and_effort(setup):
    config, _, native = setup
    seen = []

    def upstream(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=completion())

    app = create_app(config, native, httpx.MockTransport(upstream))
    response = await request(app, body())
    assert response.status_code == 200
    assert response.headers["x-codex-route"] == "omniroute"
    assert response.json()["choices"][0]["message"]["content"] == "GATEWAY ANSWER"
    assert not native.received
    assert seen[0]["messages"] == body()["messages"]
    assert seen[0]["model"] == "codex/test-model"
    assert seen[0]["reasoning_effort"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["503", "401", "disconnect", "truncated", "error_body"])
async def test_fallback_retries_same_prompt_and_saves_warning(setup, failure):
    config, profile, native = setup

    def upstream(req):
        if failure == "disconnect":
            raise httpx.ConnectError("sensitive provider details", request=req)
        if failure == "truncated":
            return httpx.Response(200, content=b'{"choices": [')
        if failure == "error_body":
            return httpx.Response(200, json={"error": {"message": "private error"}})
        return httpx.Response(int(failure), text="private upstream diagnostics")

    app = create_app(config, native, httpx.MockTransport(upstream))
    response = await request(app, body())
    content = response.json()["choices"][0]["message"]["content"]
    assert response.headers["x-codex-route"] == "direct"
    assert content == DIRECT_NOTICE + "DIRECT ANSWER"
    assert native.received == [anthropic_request(body(), profile)]
    assert native.received[0]["output_config"]["effort"] == "high"
    assert "private" not in response.text


@pytest.mark.asyncio
async def test_streaming_warning_is_content_and_has_completion_boundary(setup):
    config, _, native = setup
    app = create_app(config, native, httpx.MockTransport(lambda r: httpx.Response(502)))
    response = await request(app, body(stream=True))
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events if e["choices"])
    assert text == DIRECT_NOTICE + "DIRECT ANSWER"
    assert response.text.endswith("data: [DONE]\n\n")
    assert any(e["choices"] and e["choices"][0]["finish_reason"] == "stop" for e in events)


@pytest.mark.asyncio
async def test_interrupted_primary_body_does_not_publish_partial_answer(setup):
    config, _, native = setup

    class Partial(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"choices":[{"message":{"content":"DO NOT SHOW PARTIAL'
            raise httpx.ReadError("upstream disconnected")

    app = create_app(
        config, native, httpx.MockTransport(lambda r: httpx.Response(200, stream=Partial()))
    )
    response = await request(app, body(stream=True))
    assert "DO NOT SHOW PARTIAL" not in response.text
    assert "DIRECT ANSWER" in response.text


@pytest.mark.asyncio
async def test_direct_continuation_does_not_retry_gateway_and_strips_own_notice(setup):
    config, _, native = setup

    def upstream(req):
        pytest.fail("A recovered conversation must stay direct")

    app = create_app(config, native, httpx.MockTransport(upstream))
    payload = body()
    payload["messages"] += [
        {"role": "assistant", "content": DIRECT_NOTICE + "Earlier answer"},
        {"role": "user", "content": "Continue the original task"},
    ]
    assert direct_history(payload["messages"])
    response = await request(app, payload)
    assert response.status_code == 200
    assert native.received[0]["messages"][1]["content"] == [
        {"type": "text", "text": "Earlier answer"}
    ]


@pytest.mark.asyncio
async def test_bad_input_and_auth_do_not_invoke_fallback(setup):
    config, _, native = setup
    app = create_app(config, native, httpx.MockTransport(lambda r: httpx.Response(400)))
    assert (await request(app, body(), key="wrong")).status_code == 401
    assert (await request(app, body())).status_code == 400
    payload = body()
    payload["reasoning_effort"] = "unsupported"
    assert (await request(app, payload)).status_code == 400
    assert not native.received


def test_tool_ids_and_results_survive_notice_and_translation(setup):
    _, profile, _ = setup
    payload = body()
    payload["tools"] = [
        {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    ]
    payload["messages"] += [
        {
            "role": "assistant",
            "content": DIRECT_NOTICE,
            "tool_calls": [
                {
                    "id": "toolu_test",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"x":1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_test", "content": "Result from the original tool"},
    ]
    translated = anthropic_request(payload, profile)
    assert translated["messages"][-2]["content"] == [
        {"type": "tool_use", "id": "toolu_test", "name": "lookup", "input": {"x": 1}}
    ]
    assert translated["messages"][-1]["content"][0]["tool_use_id"] == "toolu_test"
    assert translated["tools"][0]["name"] == "lookup"


@pytest.mark.asyncio
async def test_both_failed_routes_are_visible_and_retain_prompt(setup):
    config, _, native = setup

    async def failed(*args):
        raise RuntimeError("must not expose credentials")

    native.response = failed
    app = create_app(config, native, httpx.MockTransport(lambda r: httpx.Response(503)))
    response = await request(app, body(stream=True))
    assert "Both routes failed" in response.text
    assert "original prompt remains" in response.text
    assert "must not expose" not in response.text
    assert native.discarded


@pytest.mark.asyncio
async def test_cancelled_native_call_cleans_up_pending_turn(setup):
    config, profile, native = setup
    started = asyncio.Event()

    async def hanging(*args):
        started.set()
        await asyncio.Event().wait()

    native.response = hanging
    app = create_app(config, native)
    task = asyncio.create_task(app.state.bridge.direct(body(), profile))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(native.discarded) == 1
    assert app.state.bridge.direct_slots._value == 1


def test_image_contents_are_preserved_for_native_input(setup):
    _, profile, _ = setup
    payload = body()
    payload["messages"][-1]["content"] = [
        {"type": "text", "text": "Describe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}},
    ]
    translated = anthropic_request(payload, profile)
    assert translated["messages"][-1]["content"][-1]["source"]["url"].startswith("data:image/")


@pytest.mark.asyncio
async def test_native_start_attaches_images_without_base64_in_transcript(setup):
    from omnigent.harnesses.claude_native.codex_bridge import Bridge

    _, profile, _ = setup
    seen = {}

    class Rpc:
        async def request(self, method, params):
            seen[method] = params
            return {"thread": {"id": "fixture"}, "turn": {"id": "native-turn-fixture"}}

        async def close(self):
            pass

    bridge = Bridge({"cwd": "/workspace", "client_name": "Open WebUI", "allow_images": True})

    async def connect():
        return Rpc()

    bridge.connect = connect
    payload = body()
    payload["messages"][-1]["content"] = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}}
    ]
    turn = await bridge.start(anthropic_request(payload, profile))
    inputs = seen["turn/start"]["input"]
    assert inputs[1] == {"type": "image", "url": "data:image/png;base64,ZmFrZQ=="}
    assert "Open WebUI" in inputs[0]["text"]
    assert "ZmFrZQ==" not in inputs[0]["text"]
    await bridge.discard(turn)
    assert seen["turn/interrupt"]["turnId"] == "native-turn-fixture"
