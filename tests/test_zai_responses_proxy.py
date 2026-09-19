"""Tests for the session-local Z.ai Chat-to-Responses adapter."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

from omnigent.harnesses.codex_native import zai_responses_proxy
from omnigent.harnesses.codex_native.zai_responses_proxy import (
    ZaiResponsesProxyConfig,
    _response_output_from_chat,
    responses_request_to_chat,
    start_zai_responses_proxy,
)


def test_responses_request_maps_messages_tools_and_reasoning() -> None:
    payload = responses_request_to_chat(
        {
            "model": "glm-5.3",
            "instructions": "Be concise.",
            "input": [{"type": "message", "role": "user", "content": "PONG?"}],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look something up.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "max_output_tokens": 32,
            "reasoning": {"effort": "none"},
        }
    )

    assert payload == {
        "model": "glm-5.3",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "PONG?"},
        ],
        "stream": False,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look something up.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "max_tokens": 32,
        "thinking": {"type": "disabled"},
    }


def test_chat_response_maps_text_and_usage_to_responses() -> None:
    response = _response_output_from_chat(
        {
            "choices": [{"message": {"role": "assistant", "content": "PONG"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        },
        "glm-5.3",
    )

    assert response["model"] == "glm-5.3"
    assert response["output_text"] == "PONG"
    assert response["usage"] == {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}
    assert response["output"][0]["content"][0]["text"] == "PONG"  # type: ignore[index]


def test_proxy_config_repr_does_not_expose_api_key() -> None:
    config = ZaiResponsesProxyConfig(api_key="secret-value")

    assert "secret-value" not in repr(config)


def test_proxy_dispatches_responses_request_to_zai_chat_without_leaking_key(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    class _UpstreamResponse:
        status = 200

        def __enter__(self) -> _UpstreamResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [{"message": {"role": "assistant", "content": "PONG"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                }
            ).encode()

    def fake_urlopen(request: Request, *, timeout: float) -> _UpstreamResponse:
        assert timeout == 120
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data or b"{}")
        return _UpstreamResponse()

    monkeypatch.setattr(zai_responses_proxy, "urlopen", fake_urlopen)
    adapter = start_zai_responses_proxy(ZaiResponsesProxyConfig(api_key="secret-value"))
    try:
        request = Request(
            f"{adapter.base_url}/responses",
            data=json.dumps(
                {
                    "model": "glm-5.3",
                    "input": "Reply with exactly: PONG",
                    "stream": False,
                    "max_output_tokens": 32,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    finally:
        adapter.close()

    assert seen["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert seen["authorization"] == "Bearer secret-value"
    assert seen["body"] == {
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
        "stream": False,
        "max_tokens": 32,
    }
    assert payload["output_text"] == "PONG"
