"""Lifecycle contracts for the runner-owned OpenCode provenance proxy."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.opencode_provenance import (
    OpenCodeProvenanceProxyService,
    OpenCodeProvenanceProxyUnavailable,
)


class _SseStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"


@pytest.mark.anyio
async def test_runner_owned_proxy_starts_once_forwards_and_stops() -> None:
    forwarded: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-omniroute-requested-model": "auto/coding:reliable",
                "x-omniroute-selected-provider": "anthropic",
                "x-omniroute-selected-model": "claude-test",
            },
            stream=_SseStream(),
        )

    service = OpenCodeProvenanceProxyService("http://127.0.0.1:20128/v1")
    service._proxy._client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    await service.start()
    first_url = service.base_url
    await service.start()
    assert service.base_url == first_url
    registration = service.register(
        session_id="conv_1", approved_combo="auto/coding:reliable", reasoning="high"
    )
    async with httpx.AsyncClient() as client:
        health = await client.get(f"{service.base_url}/healthz")
        response = await client.post(
            f"{registration.base_url}/chat/completions",
            content=json.dumps(
                {
                    "model": "auto/coding:reliable",
                    "reasoning_effort": "high",
                    "stream": True,
                }
            ),
            headers={"content-type": "application/json"},
        )
    assert health.json() == {"status": "ok"}
    assert response.status_code == 200
    assert response.content == b"data: first\n\ndata: [DONE]\n\n"
    assert forwarded[0].url == "http://127.0.0.1:20128/v1/chat/completions"
    assert json.loads(forwarded[0].content)["model"] == "auto/coding:reliable"
    await service.stop()
    with pytest.raises(httpx.HTTPError):
        async with httpx.AsyncClient(timeout=0.1) as client:
            await client.get(f"{first_url}/healthz")


@pytest.mark.anyio
async def test_proxy_rejects_recursive_fixed_listener_before_binding() -> None:
    service = OpenCodeProvenanceProxyService("http://127.0.0.1:29999/v1", port=29999)
    with pytest.raises(OpenCodeProvenanceProxyUnavailable, match="must not resolve"):
        await service.start()
