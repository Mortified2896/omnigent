"""Lifecycle contracts for the runner-owned OpenCode runtime provenance observer."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from omnigent.opencode_provenance_proxy import (
    ActiveExecution,
    StructuredRuntimeProvenance,
)
from omnigent.runner.opencode_provenance import OpenCodeProvenanceProxyService


class _SseBody(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"

    def close(self) -> None:  # type: ignore[no-untyped-def]
        return None


@pytest.mark.asyncio
async def test_proxy_starts_once_forwards_and_stops() -> None:
    forwarded: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-omniroute-requested-model": "auto/coding:reliable",
                "x-omniroute-selected-provider": "codex",
                "x-omniroute-selected-model": "codex/gpt-5.4-mini",
            },
            stream=httpx.ByteStream(b"data: first\n\ndata: [DONE]\n\n"),
        )

    service = OpenCodeProvenanceProxyService("http://127.0.0.1:20128/v1")
    service._proxy._client_factory = lambda: httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(upstream)
    )
    await service.start()
    first_url = service.base_url
    await service.start()
    assert service.base_url == first_url
    registration = service.register(
        session_id="conv_1",
        approved_combo="auto/coding:reliable",
        reasoning="high",
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


@pytest.mark.asyncio
async def test_register_requires_healthy_listener() -> None:
    service = OpenCodeProvenanceProxyService("http://127.0.0.1:20128/v1")
    with pytest.raises(Exception) as exc:
        service.register(
            session_id="conv_1", approved_combo="auto/coding:reliable", reasoning="high"
        )
    assert "registr" in str(exc.value) or "observ" in str(exc.value)


@pytest.mark.asyncio
async def test_clear_releases_authorization() -> None:
    service = OpenCodeProvenanceProxyService("http://127.0.0.1:20128/v1")
    service._proxy._client_factory = lambda: httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(lambda req: httpx.Response(200))
    )
    await service.start()
    service.register(session_id="conv_1", approved_combo="auto/coding:reliable", reasoning="high")
    assert "conv_1" in service._registrations
    service.clear("conv_1")
    assert "conv_1" not in service._registrations
    await service.stop()


@pytest.mark.asyncio
async def test_record_runtime_callback_receives_structured_metadata() -> None:
    captured: list[tuple[ActiveExecution, StructuredRuntimeProvenance]] = []
    service = OpenCodeProvenanceProxyService("http://127.0.0.1:20128/v1")
    service._proxy._client_factory = lambda: httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(_record_upstream)
    )
    service._proxy._record_runtime = (  # type: ignore[attr-defined]
        lambda active, provenance: captured.append((active, provenance)) or asyncio.sleep(0)
    )
    await service.start()
    registration = service.register(
        session_id="conv_x",
        approved_combo="auto/coding:reliable",
        reasoning="high",
    )
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{registration.base_url}/chat/completions",
            json={
                "model": "auto/coding:reliable",
                "reasoning_effort": "high",
            },
        )
    # Allow the recorded coroutine to drain before assertions.
    await asyncio.sleep(0)
    assert captured, "record_runtime should have been invoked"
    active, provenance = captured[-1]
    assert active.conversation_id == "conv_x"
    assert provenance.actual_provider == "minimax"
    assert provenance.actual_provider_model == "minimax/MiniMax-M3"
    assert provenance.verified is True
    assert provenance.fallback_used is False
    await service.stop()


def _record_upstream(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "content-type": "application/json",
            "x-omniroute-requested-model": "auto/coding:reliable",
            "x-omniroute-selected-provider": "minimax",
            "x-omniroute-selected-model": "minimax/MiniMax-M3",
            "x-omniroute-fallback-used": "false",
        },
    )


async def _await_done() -> None:
    return None
