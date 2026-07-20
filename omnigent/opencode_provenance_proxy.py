"""Allow-listed structured provenance for approved OpenCode OmniRoute traffic."""

from __future__ import annotations

import json
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
    "x-omniroute-provider",
    "x-omniroute-model",
    "x-omniroute-selected-provider",
    "x-omniroute-selected-model",
    "x-omniroute-decision-id",
    "x-omniroute-request-id",
    "x-omniroute-fallback-used",
    "x-omniroute-selection-strategy",
    "x-omniroute-billing-class",
)


@dataclass(frozen=True)
class ActiveExecution:
    """Approved package associated with one opaque observer path."""

    conversation_id: str
    approved_combo: str
    approved_reasoning: str


@dataclass(frozen=True)
class ModelRequest:
    """Allow-listed request metadata used to enforce the approved package."""

    model: str | None
    reasoning: str | None
    stream: bool | None


@dataclass(frozen=True)
class StructuredRuntimeProvenance:
    """Allow-listed structured metadata returned by OmniRoute."""

    requested_model: str | None
    actual_provider: str | None
    actual_provider_model: str | None
    verified: bool
    fallback_used: bool | None
    omniroute_request_id: str | None
    omniroute_decision_id: str | None
    selection_strategy: str | None
    billing_class: str | None


RuntimeRecorder = Callable[[ActiveExecution, StructuredRuntimeProvenance], Awaitable[None]]


def _nonempty(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped and stripped.lower() != "unknown" else None


def _optional_bool(value: str | None) -> bool | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def structured_runtime_provenance(
    headers: Mapping[str, str], *, approved_combo: str
) -> StructuredRuntimeProvenance:
    """Extract concrete execution identity only from structured response headers.

    The approved package's own model id (or the bare ``auto`` group) is
    not a concrete provider/model. Provenance is verified only when the
    structured upstream selection actually names a provider+model distinct
    from the approved combo.
    """
    requested = _nonempty(headers.get("x-omniroute-requested-model"))
    provider = _nonempty(
        headers.get("x-omniroute-selected-provider") or headers.get("x-omniroute-provider")
    )
    model = _nonempty(
        headers.get("x-omniroute-selected-model") or headers.get("x-omniroute-model")
    )
    lowered_model = model.lower() if model is not None else ""
    verified = bool(
        provider
        and model
        and model != approved_combo
        and lowered_model != "auto"
        and not lowered_model.startswith("auto/")
    )
    return StructuredRuntimeProvenance(
        requested_model=requested,
        actual_provider=provider if verified else None,
        actual_provider_model=model if verified else None,
        verified=verified,
        fallback_used=_optional_bool(headers.get("x-omniroute-fallback-used")),
        omniroute_request_id=_nonempty(headers.get("x-omniroute-request-id")),
        omniroute_decision_id=_nonempty(headers.get("x-omniroute-decision-id")),
        selection_strategy=_nonempty(headers.get("x-omniroute-selection-strategy")),
        billing_class=_nonempty(headers.get("x-omniroute-billing-class")),
    )


class OpenCodeProvenanceProxy:
    """Fail-closed observer proxy for approved OpenCode OmniRoute requests."""

    def __init__(
        self,
        upstream_base_url: str,
        *,
        record_runtime: RuntimeRecorder | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._upstream = upstream_base_url.rstrip("/")
        self._active: dict[str, ActiveExecution] = {}
        self._record_runtime = record_runtime
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)
            )
        )

    def register(self, bridge_id: str, active: ActiveExecution) -> None:
        """Install or replace the package authorized for an opaque path."""
        self._active[bridge_id] = active

    def clear(self, bridge_id: str, conversation_id: str) -> None:
        """Remove an opaque path only when it still belongs to the session."""
        active = self._active.get(bridge_id)
        if active is not None and active.conversation_id == conversation_id:
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
        try:
            value = json.loads(body)
            if isinstance(value, dict) and isinstance(value.get("error"), dict):
                code = value["error"].get("code")
                return str(code)[:64] if code else "upstream_error"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return "upstream_error"

    async def _record(self, active: ActiveExecution, captured: Mapping[str, str]) -> None:
        import logging

        logging.getLogger(__name__).info(
            "opencode provenance observer captured session=%s headers=%s",
            active.conversation_id,
            sorted(captured.keys()),
        )
        if self._record_runtime is None:
            return
        provenance = structured_runtime_provenance(captured, approved_combo=active.approved_combo)
        try:
            await self._record_runtime(active, provenance)
        except Exception:  # noqa: BLE001 - execution must not lose its response.
            import logging

            logging.getLogger(__name__).warning(
                "Could not persist OpenCode runtime provenance for session=%s",
                active.conversation_id,
                exc_info=True,
            )

    def app(self) -> FastAPI:
        """Build the loopback ASGI application."""
        app = FastAPI()

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @app.api_route("/{bridge_id}/{path:path}", methods=["POST"])
        async def forward(bridge_id: str, path: str, request: Request) -> Response:
            import logging

            logging.getLogger(__name__).info(
                "opencode provenance observer forwarding bridge_id=%s path=%s method=%s",
                bridge_id[:8],
                path,
                request.method,
            )
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
                            "code": "missing_approved_execution",
                            "message": "No approved execution is registered",
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
            if (
                metadata.reasoning is not None
                and active.approved_reasoning
                and metadata.reasoning != active.approved_reasoning
            ):
                return JSONResponse(
                    {
                        "error": {
                            "code": "approved_reasoning_mismatch",
                            "message": "Requested reasoning does not match approval",
                        }
                    },
                    status_code=422,
                )
            headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() not in _HOP_BY_HOP
            }
            upstream_path = (
                route_path[3:]
                if self._upstream.endswith("/v1") and route_path.startswith("/v1/")
                else route_path
            )
            target = f"{self._upstream}{upstream_path}" + (
                f"?{request.url.query}" if request.url.query else ""
            )
            client = self._client_factory()
            try:
                upstream = await client.send(
                    client.build_request(request.method, target, headers=headers, content=body),
                    stream=True,
                )
            except httpx.HTTPError:
                await client.aclose()
                return JSONResponse(
                    {
                        "error": {
                            "code": "upstream_unreachable",
                            "message": "OmniRoute could not be reached",
                        }
                    },
                    status_code=502,
                )
            return await self._handle_upstream(active, upstream, metadata)

        return app

    async def _handle_upstream(
        self,
        active: ActiveExecution,
        upstream: httpx.Response,
        metadata: ModelRequest,
    ) -> Response:
        """Forward the response to the OpenCode client and capture provenance."""
        captured = {
            name: upstream.headers[name]
            for name in _PROVENANCE_HEADERS
            if name in upstream.headers
        }
        if upstream.is_success:
            await self._record(active, captured)
        else:
            failure = self._safe_error(await upstream.aread())
            return JSONResponse(
                {"error": {"code": failure, "message": "OmniRoute request failed"}},
                status_code=upstream.status_code,
            )
        response_headers = {
            key: value for key, value in upstream.headers.items() if key.lower() not in _HOP_BY_HOP
        }
        if metadata.stream is not True:
            content = await upstream.aread()
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=response_headers,
            )

        async def stream() -> Any:
            try:
                pending_metadata: dict[str, str] = {}
                buffer = b""
                async for chunk in upstream.aiter_raw():
                    yield chunk
                    buffer += chunk
                    # OmniRoute streams provenance as SSE comment lines
                    # at the end of the body (``: x-omniroute-provider=foo``).
                    # Parse them out so ``_record`` can verify the
                    # concrete provider/model once the body is fully read.
                    while b"\n" in buffer:
                        line, _, buffer = buffer.partition(b"\n")
                        stripped = line.strip()
                        if not stripped.startswith(b":"):
                            continue
                        head = stripped[1:].strip()
                        if b"=" not in head:
                            continue
                        name, _, value = head.partition(b"=")
                        try:
                            name_str = name.decode("ascii").strip().lower()
                            value_str = value.decode("utf-8").strip()
                        except UnicodeDecodeError:
                            continue
                        if name_str in _PROVENANCE_HEADERS and value_str:
                            pending_metadata[name_str] = value_str
                if pending_metadata:
                    await self._record(active, pending_metadata)
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            headers=response_headers,
        )
