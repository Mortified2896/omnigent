"""Focused contracts for the transparent OpenCode provenance proxy."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.opencode_provenance_proxy import ActiveTaskRun, ProvenanceProxy


class _SseStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"


@pytest.fixture
def active() -> ActiveTaskRun:
    return ActiveTaskRun(
        task_run_id="tr_1",
        conversation_id="conv_1",
        approved_combo="auto/coding:reliable",
        approved_reasoning="high",
        opencode_session_id="oc_1",
        correlation_id="opaque-registration-token",
    )


def _proxy(handler, **kwargs):  # type: ignore[no-untyped-def]
    return ProvenanceProxy(
        "https://omniroute.example/v1",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


@pytest.mark.anyio
async def test_health_and_stream_are_transparent_and_capture_execution_headers(active) -> None:  # type: ignore[no-untyped-def]
    starts = []
    finishes = []

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://omniroute.example/v1/chat/completions"
        assert json.loads(request.content)["model"] == active.approved_combo
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-omniroute-requested-model": active.approved_combo,
                "x-omniroute-selected-provider": "anthropic",
                "x-omniroute-selected-model": "claude-test",
                "x-omniroute-request-id": "request-1",
                "x-omniroute-decision-id": "decision-1",
            },
            stream=_SseStream(),
        )

    async def started(*args):  # type: ignore[no-untyped-def]
        starts.append(args)

    async def finished(*args):  # type: ignore[no-untyped-def]
        finishes.append(args)

    proxy = _proxy(upstream, record_start=started, record_finish=finished)
    proxy.register("bridge-secret", active)
    transport = httpx.ASGITransport(app=proxy.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        response = await client.post(
            "/bridge-secret/v1/chat/completions",
            json={"model": active.approved_combo, "reasoning_effort": "high", "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b"data: first\n\ndata: [DONE]\n\n"
    assert starts[0][1] == "opaque-registration-token"
    assert starts[0][2].model == active.approved_combo
    assert finishes == [
        (
            active,
            "opaque-registration-token",
            {
                "x-omniroute-requested-model": active.approved_combo,
                "x-omniroute-selected-provider": "anthropic",
                "x-omniroute-selected-model": "claude-test",
                "x-omniroute-decision-id": "decision-1",
                "x-omniroute-request-id": "request-1",
            },
            200,
            None,
        )
    ]


@pytest.mark.anyio
async def test_non_streaming_response_is_forwarded_unchanged(active) -> None:  # type: ignore[no-untyped-def]
    body = b'{"id":"response_1","output":[]}'

    class _JsonStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield body

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://omniroute.example/v1/responses"
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=_JsonStream()
        )

    proxy = _proxy(upstream)
    proxy.register("bridge-secret", active)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/bridge-secret/v1/responses",
            json={"model": active.approved_combo, "reasoning_effort": "high"},
        )

    assert response.status_code == 200
    assert response.content == body


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("registered", "model", "reasoning", "status", "code"),
    [
        (False, "auto/coding:reliable", "high", 409, "missing_task_run"),
        (True, "other/model", "high", 422, "approved_combo_mismatch"),
        (True, "auto/coding:reliable", None, 422, "approved_reasoning_mismatch"),
    ],
)
async def test_invalid_request_fails_before_upstream_dispatch(  # type: ignore[no-untyped-def]
    active, registered, model, reasoning, status, code
) -> None:
    dispatched = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal dispatched
        dispatched = True
        return httpx.Response(200)

    proxy = _proxy(upstream)
    if registered:
        proxy.register("bridge-secret", active)
    payload = {"model": model}
    if reasoning is not None:
        payload["reasoning_effort"] = reasoning
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://proxy"
    ) as client:
        response = await client.post("/bridge-secret/v1/chat/completions", json=payload)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert not dispatched


@pytest.mark.anyio
async def test_unsupported_paths_do_not_fall_through_to_a_provider(active) -> None:  # type: ignore[no-untyped-def]
    proxy = _proxy(lambda request: pytest.fail("upstream must not be called"))
    proxy.register("bridge-secret", active)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://proxy"
    ) as client:
        response = await client.post("/bridge-secret/v1/models", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_endpoint"
