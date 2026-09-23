"""Fail-closed write admission fence used during external deployment."""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.deployment_fence import write_fence_active

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _fence_response() -> dict[str, Any]:
    return {
        "error": {
            "code": "deployment_write_fenced",
            "message": "Writes are temporarily closed while this instance is being validated.",
        }
    }


class DeploymentWriteFenceMiddleware:
    """Block all HTTP/WebSocket write admission while the host controller fences."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            if write_fence_active():
                await send(
                    {"type": "websocket.close", "code": 1013, "reason": "deployment fenced"}
                )
                return

            async def fenced_receive() -> Any:
                # Existing host/runner/browser sockets must not remain an
                # unbounded writer after the controller creates the fence.
                if write_fence_active():
                    await send(
                        {
                            "type": "websocket.close",
                            "code": 1013,
                            "reason": "deployment fenced",
                        }
                    )
                    return {"type": "websocket.disconnect", "code": 1013}
                return await receive()

            await self.app(scope, fenced_receive, send)
            return
        if not write_fence_active():
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "")).upper()
        if method not in _MUTATING_METHODS:
            await self.app(scope, receive, send)
            return
        body = json.dumps(_fence_response(), separators=(",", ":")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 423,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
