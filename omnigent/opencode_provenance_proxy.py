"""Transparent, fail-closed OpenCode to OmniRoute provenance proxy.

The proxy deliberately understands only OpenAI-compatible chat/responses paths.
It extracts a small allow-listed metadata projection for validation and auditing;
the original request bytes are forwarded unchanged and response bytes are never
inspected or persisted.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

_ALLOWED_PATHS = frozenset({"/v1/chat/completions", "/v1/responses"})
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)
_PROVENANCE_HEADERS = (
    "x-omniroute-requested-model",
    "x-omniroute-selected-provider",
    "x-omniroute-selected-model",
    "x-omniroute-decision-id",
    "x-omniroute-request-id",
    "x-omniroute-fallback-used",
    "x-omniroute-selection-strategy",
    "x-omniroute-billing-class",
)


@dataclass(frozen=True)
class ActiveTaskRun:
    task_run_id: str
    conversation_id: str
    approved_combo: str
    approved_reasoning: str
    opencode_session_id: str | None = None
    # A bridge-bound opaque token, generated when the execution is registered.
    # It is carried only in the loopback URL, never in prompt content.
    correlation_id: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    model: str | None
    reasoning: str | None
    stream: bool | None


class ProvenanceProxy:
    """Per-bridge request registry and ASGI endpoint factory.

    ``record_start`` and ``record_finish`` receive only metadata. They are
    intentionally injected so this transport layer cannot write prompts or
    credentials to a store.
    """

    def __init__(
        self,
        upstream_base_url: str,
        *,
        record_start: Callable[[ActiveTaskRun, str, ModelRequest], Awaitable[None]] | None = None,
        record_finish: Callable[
            [ActiveTaskRun, str, Mapping[str, str], int, str | None], Awaitable[None]
        ]
        | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._upstream = upstream_base_url.rstrip("/")
        self._active: dict[str, ActiveTaskRun] = {}
        self._record_start = record_start
        self._record_finish = record_finish
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)
            )
        )

    def register(self, bridge_id: str, active: ActiveTaskRun) -> None:
        self._active[bridge_id] = active

    def clear(self, bridge_id: str, task_run_id: str) -> None:
        if (
            self._active.get(bridge_id, None)
            and self._active[bridge_id].task_run_id == task_run_id
        ):
            self._active.pop(bridge_id, None)

    @staticmethod
    def _metadata(body: bytes) -> ModelRequest:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ModelRequest(None, None, None)
        if not isinstance(payload, dict):
            return ModelRequest(None, None, None)
        reasoning = payload.get("reasoning_effort") or payload.get("variant")
        if not isinstance(reasoning, str):
            options = payload.get("provider_options") or payload.get("providerOptions")
            reasoning = options.get("reasoning_effort") if isinstance(options, dict) else None
        return ModelRequest(
            payload.get("model") if isinstance(payload.get("model"), str) else None,
            reasoning if isinstance(reasoning, str) else None,
            payload.get("stream") if isinstance(payload.get("stream"), bool) else None,
        )

    @staticmethod
    def _safe_error(body: bytes) -> str:
        # Never retain upstream content. Keep only a bounded generic category.
        try:
            value = json.loads(body)
            if isinstance(value, dict) and isinstance(value.get("error"), dict):
                code = value["error"].get("code")
                return str(code)[:64] if code else "upstream_error"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return "upstream_error"

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @app.api_route("/{bridge_id}/{path:path}", methods=["POST"])
        async def forward(bridge_id: str, path: str, request: Request) -> Response:
            route_path = "/" + path
            if route_path not in _ALLOWED_PATHS:
                return JSONResponse(
                    {
                        "error": {
                            "code": "unsupported_endpoint",
                            "message": "Unsupported OpenCode OmniRoute endpoint",
                        }
                    },
                    status_code=400,
                )
            active = self._active.get(bridge_id)
            if active is None:
                return JSONResponse(
                    {
                        "error": {
                            "code": "missing_task_run",
                            "message": "No active task run is registered",
                        }
                    },
                    status_code=409,
                )
            body = await request.body()
            metadata = self._metadata(body)
            if metadata.model is None:
                return JSONResponse(
                    {
                        "error": {
                            "code": "missing_model",
                            "message": "Approved execution requires a model pin",
                        }
                    },
                    status_code=422,
                )
            if metadata.model != active.approved_combo:
                return JSONResponse(
                    {
                        "error": {
                            "code": "approved_combo_mismatch",
                            "message": "Requested model does not match approved combo",
                        }
                    },
                    status_code=422,
                )
            if metadata.reasoning is None or metadata.reasoning != active.approved_reasoning:
                return JSONResponse(
                    {
                        "error": {
                            "code": "approved_reasoning_mismatch",
                            "message": "Requested reasoning does not match approval",
                        }
                    },
                    status_code=422,
                )
            # A registration supplies an opaque, bridge-bound correlation. The
            # fallback is retained for callers upgrading independently, but all
            # runner-owned registrations provide it before dispatch.
            correlation_id = active.correlation_id or uuid.uuid4().hex
            if self._record_start:
                await self._record_start(active, correlation_id, metadata)
            headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
            # OpenAI-compatible upstream configuration commonly includes
            # ``/v1`` already. Preserve OpenCode's path without producing
            # ``/v1/v1/...`` when that is the configured OmniRoute base URL.
            upstream_path = (
                route_path[3:]
                if self._upstream.endswith("/v1") and route_path.startswith("/v1/")
                else route_path
            )
            target = f"{self._upstream}{upstream_path}" + (
                f"?{request.url.query}" if request.url.query else ""
            )
            try:
                client = self._client_factory()
                upstream = await client.send(
                    client.build_request(request.method, target, headers=headers, content=body),
                    stream=True,
                )
            except httpx.HTTPError:
                if self._record_finish:
                    await self._record_finish(
                        active, correlation_id, {}, 502, "upstream_unreachable"
                    )
                return JSONResponse(
                    {
                        "error": {
                            "code": "upstream_unreachable",
                            "message": "OmniRoute could not be reached",
                        }
                    },
                    status_code=502,
                )
            captured = {
                name: upstream.headers[name]
                for name in _PROVENANCE_HEADERS
                if name in upstream.headers
            }
            failure = None if upstream.is_success else self._safe_error(await upstream.aread())
            if failure is not None:
                await upstream.aclose()
                await client.aclose()
                if self._record_finish:
                    await self._record_finish(
                        active, correlation_id, captured, upstream.status_code, failure
                    )
                return Response(
                    content=(
                        b'{"error":{"code":"upstream_error","message":"OmniRoute request failed"}}'
                    ),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"),
                )
            response_headers = {
                k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
            }
            if metadata.stream is False:
                content = await upstream.aread()
                await upstream.aclose()
                await client.aclose()
                if self._record_finish:
                    await self._record_finish(
                        active, correlation_id, captured, upstream.status_code, None
                    )
                return Response(
                    content=content,
                    status_code=upstream.status_code,
                    headers=response_headers,
                    media_type=upstream.headers.get("content-type"),
                )

            async def stream() -> Any:
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()
                    if self._record_finish:
                        await self._record_finish(
                            active, correlation_id, captured, upstream.status_code, None
                        )

            return StreamingResponse(
                stream(),
                status_code=upstream.status_code,
                headers=response_headers,
                media_type=upstream.headers.get("content-type"),
            )

        return app
