"""Runner-owned loopback observer for approved OpenCode OmniRoute traffic."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import uvicorn

from omnigent.opencode_provenance_proxy import (
    ActiveExecution,
    OpenCodeProvenanceProxy,
    StructuredRuntimeProvenance,
)


class OpenCodeProvenanceProxyUnavailable(RuntimeError):
    """Raised when approved OpenCode traffic cannot use the observer."""


@dataclass(frozen=True)
class ProxyRegistration:
    """Opaque loopback endpoint assigned to one approved session."""

    bridge_id: str
    base_url: str


RuntimeRecorder = Callable[[ActiveExecution, StructuredRuntimeProvenance], Awaitable[None]]


def _same_listener(left: str, right: str) -> bool:
    left_parts, right_parts = urlsplit(left), urlsplit(right)
    return (
        left_parts.scheme == right_parts.scheme
        and left_parts.hostname == right_parts.hostname
        and (left_parts.port or (443 if left_parts.scheme == "https" else 80))
        == (right_parts.port or (443 if right_parts.scheme == "https" else 80))
    )


class OpenCodeProvenanceProxyService:
    """One non-blocking runtime observer owned by a runner process."""

    def __init__(
        self,
        upstream_base_url: str,
        *,
        port: int = 0,
        record_runtime: RuntimeRecorder | None = None,
    ) -> None:
        self._upstream_base_url = upstream_base_url.rstrip("/")
        self._port = port
        self._proxy = OpenCodeProvenanceProxy(
            self._upstream_base_url, record_runtime=record_runtime
        )
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._base_url: str | None = None
        self._registrations: dict[str, ProxyRegistration] = {}

    @property
    def upstream_base_url(self) -> str:
        """Return the real OmniRoute upstream URL."""
        return self._upstream_base_url

    @property
    def base_url(self) -> str:
        """Return the healthy loopback URL."""
        if self._base_url is None:
            raise OpenCodeProvenanceProxyUnavailable(
                "OpenCode provenance observer is unavailable."
            )
        return self._base_url

    async def start(self) -> None:
        """Start one loopback listener and verify its health endpoint."""
        if self._task is not None and not self._task.done():
            await self._check_health()
            return
        if self._port and _same_listener(
            self._upstream_base_url, f"http://127.0.0.1:{self._port}"
        ):
            raise OpenCodeProvenanceProxyUnavailable(
                "OmniRoute upstream must not resolve to the provenance observer."
            )
        config = uvicorn.Config(
            self._proxy.app(),
            host="127.0.0.1",
            port=self._port,
            log_level="warning",
            lifespan="off",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(), name="opencode-provenance-observer")
        for _ in range(100):
            if self._server.started and self._server.servers:
                listener = next(iter(self._server.servers)).sockets[0]
                self._base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
                if _same_listener(self._upstream_base_url, self._base_url):
                    await self.stop()
                    raise OpenCodeProvenanceProxyUnavailable(
                        "OmniRoute upstream must not resolve to the provenance observer."
                    )
                await self._check_health()
                return
            if self._task.done():
                await self._task
            await asyncio.sleep(0.01)
        await self.stop()
        raise OpenCodeProvenanceProxyUnavailable(
            "OpenCode provenance observer did not become healthy."
        )

    async def _check_health(self) -> None:
        parts = urlsplit(self.base_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        loop = asyncio.get_running_loop()

        def _probe() -> None:
            with socket.create_connection((host, port), timeout=1.0) as sock:
                sock.sendall(
                    b"GET /healthz HTTP/1.1\r\n"
                    b"Host: " + host.encode("ascii") + b"\r\n"
                    b"Connection: close\r\n\r\n"
                )
                payload = b""
                while chunk := sock.recv(4096):
                    payload += chunk
            if b"200 OK" not in payload or b'"status":"ok"' not in payload:
                raise OpenCodeProvenanceProxyUnavailable(
                    "OpenCode provenance observer health check failed."
                )

        try:
            await loop.run_in_executor(None, _probe)
        except (OSError, OpenCodeProvenanceProxyUnavailable):
            raise OpenCodeProvenanceProxyUnavailable(
                "OpenCode provenance observer health check failed."
            ) from None

    def register(
        self, *, session_id: str, approved_combo: str, reasoning: str
    ) -> ProxyRegistration:
        """Authorize one approved package before OpenCode dispatch."""
        if not approved_combo or not reasoning or self._base_url is None:
            raise OpenCodeProvenanceProxyUnavailable(
                "Approved OpenCode provenance registration failed."
            )
        existing = self._registrations.get(session_id)
        bridge_id = existing.bridge_id if existing is not None else secrets.token_urlsafe(32)
        self._proxy.register(
            bridge_id,
            ActiveExecution(
                conversation_id=session_id,
                approved_combo=approved_combo,
                approved_reasoning=reasoning,
            ),
        )
        registration = ProxyRegistration(
            bridge_id=bridge_id, base_url=f"{self.base_url}/{bridge_id}/v1"
        )
        self._registrations[session_id] = registration
        return registration

    def clear(self, session_id: str) -> None:
        """Remove a session's observer authorization."""
        registration = self._registrations.pop(session_id, None)
        if registration is not None:
            self._proxy.clear(registration.bridge_id, session_id)

    async def stop(self) -> None:
        """Stop the in-process listener and release its socket."""
        for session_id in list(self._registrations):
            self.clear(session_id)
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._server = None
        self._task = None
        self._base_url = None
