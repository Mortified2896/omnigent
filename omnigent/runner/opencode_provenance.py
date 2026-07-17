"""Runner-owned loopback server for approved OpenCode OmniRoute traffic."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import uvicorn

from omnigent.opencode_provenance_proxy import ActiveTaskRun, ProvenanceProxy


class OpenCodeProvenanceProxyUnavailable(RuntimeError):
    """Raised when approved OpenCode traffic cannot use the proxy."""


@dataclass(frozen=True)
class ProxyRegistration:
    """Opaque loopback endpoint assigned to one approved bridge."""

    bridge_id: str
    base_url: str


def _same_listener(left: str, right: str) -> bool:
    """Return whether two URLs identify the same loopback listener."""
    left_parts, right_parts = urlsplit(left), urlsplit(right)
    return (
        left_parts.scheme == right_parts.scheme
        and left_parts.hostname == right_parts.hostname
        and (left_parts.port or (443 if left_parts.scheme == "https" else 80))
        == (right_parts.port or (443 if right_parts.scheme == "https" else 80))
    )


class OpenCodeProvenanceProxyService:
    """One non-blocking provenance proxy owned by a runner process.

    OmniRoute credentials remain in the existing per-session OpenCode provider
    configuration. The proxy forwards that authorization header upstream but
    never logs or persists it.
    """

    def __init__(self, upstream_base_url: str, *, port: int = 0) -> None:
        self._upstream_base_url = upstream_base_url.rstrip("/")
        self._port = port
        self._proxy = ProvenanceProxy(self._upstream_base_url)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._base_url: str | None = None
        self._registrations: dict[str, ProxyRegistration] = {}

    @property
    def upstream_base_url(self) -> str:
        """Real OmniRoute upstream URL; never the proxy URL."""
        return self._upstream_base_url

    @property
    def base_url(self) -> str:
        """Loopback proxy URL after a healthy start."""
        if self._base_url is None:
            raise OpenCodeProvenanceProxyUnavailable("OpenCode provenance proxy is unavailable.")
        return self._base_url

    async def start(self) -> None:
        """Start exactly one loopback listener and verify its health endpoint."""
        if self._task is not None and not self._task.done():
            await self._check_health()
            return
        if self._port and _same_listener(
            self._upstream_base_url, f"http://127.0.0.1:{self._port}"
        ):
            raise OpenCodeProvenanceProxyUnavailable(
                "OmniRoute upstream must not resolve to the provenance proxy."
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
        self._task = asyncio.create_task(self._server.serve(), name="opencode-provenance-proxy")
        for _ in range(100):
            if self._server.started and self._server.servers:
                socket = next(iter(self._server.servers)).sockets[0]
                port = socket.getsockname()[1]
                self._base_url = f"http://127.0.0.1:{port}"
                if _same_listener(self._upstream_base_url, self._base_url):
                    await self.stop()
                    raise OpenCodeProvenanceProxyUnavailable(
                        "OmniRoute upstream must not resolve to the provenance proxy."
                    )
                await self._check_health()
                return
            if self._task.done():
                await self._task
            await asyncio.sleep(0.01)
        await self.stop()
        raise OpenCodeProvenanceProxyUnavailable(
            "OpenCode provenance proxy did not become healthy."
        )

    async def _check_health(self) -> None:
        # Use a lightweight stdlib socket probe rather than ``httpx.AsyncClient``
        # so this path is unaffected by runner-level monkey patches and stays out
        # of the network-client bookkeeping that startup tests assert against.
        parts = urlsplit(self.base_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        loop = asyncio.get_running_loop()

        def _probe() -> None:
            with socket.create_connection((host, port), timeout=1.0) as sock:
                sock.sendall(
                    b"GET /healthz HTTP/1.1\r\n"
                    b"Host: " + host.encode("ascii") + b"\r\n"
                    b"Connection: close\r\n\r\n"
                )
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            payload = b"".join(chunks)
            if b"200 OK" not in payload or b'"status":"ok"' not in payload:
                raise OpenCodeProvenanceProxyUnavailable(
                    "OpenCode provenance proxy health check failed."
                )

        try:
            await loop.run_in_executor(None, _probe)
        except (OSError, OpenCodeProvenanceProxyUnavailable):
            raise OpenCodeProvenanceProxyUnavailable(
                "OpenCode provenance proxy health check failed."
            ) from None
        return

    def register(
        self, *, session_id: str, approved_combo: str, reasoning: str
    ) -> ProxyRegistration:
        """Authorize one approved bridge before its OpenCode request can run."""
        if not approved_combo or not reasoning or self._base_url is None:
            raise OpenCodeProvenanceProxyUnavailable(
                "Approved OpenCode proxy registration failed."
            )
        existing = self._registrations.get(session_id)
        bridge_id = existing.bridge_id if existing is not None else secrets.token_urlsafe(32)
        active = ActiveTaskRun(
            task_run_id=f"runtime:{session_id}",
            conversation_id=session_id,
            approved_combo=approved_combo,
            approved_reasoning=reasoning,
            correlation_id=bridge_id,
        )
        self._proxy.register(bridge_id, active)
        registration = ProxyRegistration(
            bridge_id=bridge_id, base_url=f"{self.base_url}/{bridge_id}/v1"
        )
        self._registrations[session_id] = registration
        return registration

    def clear(self, session_id: str) -> None:
        """Remove a bridge authorization when its session stops."""
        registration = self._registrations.pop(session_id, None)
        if registration is not None:
            self._proxy.clear(registration.bridge_id, f"runtime:{session_id}")

    async def stop(self) -> None:
        """Stop the in-process server and release its loopback socket."""
        self._registrations.clear()
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._server = None
        self._task = None
        self._base_url = None
