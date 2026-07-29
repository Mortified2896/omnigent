"""Integration tests for the runner WebSocket tunnel route."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import partial

import httpx
import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

from omnigent.errors import OmnigentError
from omnigent.runner import create_runner_app
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    RequestFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.serve import dispatch_via_asgi
from omnigent.runner.transports.ws_tunnel.transport import (
    WSTunnelTransport,
)
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from tests.runner.helpers import NullServerClient

pytestmark = pytest.mark.asyncio

_RUNNER_ID = "runner-route-test-1"
_TUNNEL_PATH = f"/v1/runners/{_RUNNER_ID}/tunnel"


@dataclass(frozen=True)
class RoutedTunnelClient:
    """Client and registry wired through the production tunnel route.

    :param client: HTTP client backed by :class:`WSTunnelTransport`.
    :param registry: Tunnel registry owned by the FastAPI route.
    """

    client: httpx.AsyncClient
    registry: TunnelRegistry


@dataclass(frozen=True)
class TunnelRouteApp:
    """Minimal app and registry for tunnel route tests.

    :param app: FastAPI app containing only the tunnel route.
    :param registry: Tunnel registry owned by the route.
    """

    app: FastAPI
    registry: TunnelRegistry


def _websocket_scope(
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client_host: str = "127.0.0.1",
) -> dict[str, object]:
    """Build an ASGI WebSocket scope for a test path.

    :param path: WebSocket path, e.g.
        ``"/v1/runners/runner-route-test-1/tunnel"``.
    :param headers: Optional ASGI handshake headers.
    :param client_host: ASGI client host, e.g. ``"127.0.0.1"``.
    :returns: A minimal ASGI WebSocket scope accepted by FastAPI.
    """
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers or [],
        "client": (client_host, 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


async def _connect_route(
    app: FastAPI,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client_host: str = "127.0.0.1",
) -> ApplicationCommunicator:
    """Connect an ASGI WebSocket communicator to the tunnel route.

    :param app: FastAPI app containing the runner tunnel router.
    :param path: WebSocket path, e.g.
        ``"/v1/runners/runner-route-test-1/tunnel"``.
    :param headers: Optional ASGI handshake headers.
    :param client_host: ASGI client host, e.g. ``"127.0.0.1"``.
    :returns: The connected ASGI communicator.
    """
    communicator = ApplicationCommunicator(
        app,
        _websocket_scope(path, headers=headers, client_host=client_host),
    )
    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept", (
        f"Expected {path} to accept the WebSocket route; got {accepted!r}. "
        "If this is a websocket.close frame, the route is probably mounted "
        "under the wrong prefix."
    )
    return communicator


def _tunnel_route_app(
    *,
    allowed_tunnel_tokens: frozenset[str] | None = None,
    auth_provider: AuthProvider | None = None,
    resolve_managed_runner_owner: Callable[[str], str | None] | None = None,
) -> TunnelRouteApp:
    """Create a minimal app containing only the runner tunnel route.

    :param allowed_tunnel_tokens: Optional token allow-list passed to
        the route, e.g. ``frozenset({"current-token"})``.
    :param auth_provider: Optional auth provider wired into the route.
        ``None`` keeps the no-auth single-user posture; a provider
        activates owner recording and the fail-closed gate.
    :param resolve_managed_runner_owner: Optional ``runner_id -> owner``
        resolver for server-managed sandbox runners (binding-token auth,
        no user session). ``None`` disables the managed-runner lookup.
    :returns: The FastAPI app and registry owned by its route.
    """
    registry = TunnelRegistry()
    app = FastAPI()
    app.state.tunnel_registry = registry
    app.include_router(
        create_runner_tunnel_router(
            registry,
            allowed_tunnel_tokens=allowed_tunnel_tokens,
            auth_provider=auth_provider,
            resolve_managed_runner_owner=resolve_managed_runner_owner,
        ),
        prefix="/v1",
    )
    return TunnelRouteApp(app=app, registry=registry)


class _CredentialHeaderAuthProvider(AuthProvider):
    """Real auth provider stub modeling the OIDC / accounts contract.

    Returns the user id carried by a credential header when present,
    and ``None`` otherwise — exactly how ``UnifiedAuthProvider``
    behaves in ``oidc`` / ``accounts`` mode (the deployed
    Databricks-OAuth posture), where a missing or invalid cookie /
    Bearer yields ``None``. This is deliberately *not* header mode,
    which falls back to :data:`RESERVED_USER_LOCAL` on a missing
    header and so never produces the ``None`` that the fail-closed gate turns on.

    A real ``AuthProvider`` subclass (not a ``MagicMock``) so the
    route's ``auth_provider is not None`` and ``isinstance`` checks
    behave like production and the exact fail-closed branch is
    exercised — without minting real JWT cookies.

    :param credential_header: Lowercase handshake header carrying the
        resolved identity, e.g. ``"x-test-user"``.
    """

    def __init__(self, credential_header: str = "x-test-user") -> None:
        self._credential_header = credential_header

    def get_user_id(self, request: HTTPConnection) -> str | None:
        """Return the identity from the credential header, or ``None``.

        :param request: Incoming HTTP request or WebSocket handshake.
        :returns: The header value, e.g. ``"alice@example.com"``, or
            ``None`` when the header is absent (unauthenticated peer).
        """
        return request.headers.get(self._credential_header)


async def _send_hello(
    communicator: ApplicationCommunicator,
    registry: TunnelRegistry,
    *,
    runner_id: str = _RUNNER_ID,
) -> None:
    """Send the runner hello frame.

    :param communicator: Connected ASGI WebSocket communicator.
    :param registry: Registry shared with the tunnel router.
    :param runner_id: Runner id expected to register.
    :returns: None.
    """
    hello = HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=["claude-sdk"],
        envs=["os_sandbox"],
    )
    await communicator.send_input(
        {"type": "websocket.receive", "text": encode_frame(hello)},
    )

    await asyncio.wait_for(_wait_until_registered(registry, runner_id), timeout=1.0)


async def _wait_until_registered(registry: TunnelRegistry, runner_id: str) -> None:
    """Wait for runner registration.

    :param registry: Registry shared with the tunnel router.
    :param runner_id: Runner id expected to register.
    :returns: None.
    """
    while registry.get(runner_id) is None:
        await asyncio.sleep(0.01)


async def _route_requests_to_runner(
    communicator: ApplicationCommunicator,
    runner_app: FastAPI,
) -> None:
    """Forward request frames into the runner ASGI app.

    :param communicator: Connected ASGI WebSocket communicator.
    :param runner_app: Runner FastAPI app receiving requests.
    :returns: None.
    """
    while True:
        output = await communicator.receive_output()
        if output["type"] == "websocket.close":
            return
        if output["type"] != "websocket.send":
            continue

        frame = decode_frame(output["text"])
        if not isinstance(frame, RequestFrame):
            continue

        await dispatch_via_asgi(
            runner_app,
            frame,
            partial(_send_response_frame, communicator),
        )


async def _send_response_frame(
    communicator: ApplicationCommunicator,
    text: str,
) -> None:
    """Send a response frame back into the tunnel route.

    :param communicator: Connected ASGI WebSocket communicator.
    :param text: Encoded response frame JSON.
    :returns: None.
    """
    await communicator.send_input({"type": "websocket.receive", "text": text})


@pytest.fixture
async def routed_tunnel_client(app: FastAPI) -> AsyncIterator[RoutedTunnelClient]:
    """Yield a client dispatching through the real WS route.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :yields: A :class:`RoutedTunnelClient` where ``client`` sends
        requests through ``WSTunnelTransport``.
    """
    registry = app.state.tunnel_registry

    communicator = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello(communicator, registry)

    runner_app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    route_task = asyncio.create_task(
        _route_requests_to_runner(communicator, runner_app),
        name="runner-tunnel-route-test-forwarder",
    )
    client = httpx.AsyncClient(
        transport=WSTunnelTransport(registry, _RUNNER_ID),
        base_url="http://runner",
    )

    try:
        yield RoutedTunnelClient(client=client, registry=registry)
    finally:
        await client.aclose()
        route_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await route_task
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


async def test_ws_tunnel_route_round_trips_request_to_runner(
    routed_tunnel_client: RoutedTunnelClient,
) -> None:
    """GET /health must round-trip through the real FastAPI WS route.

    :param routed_tunnel_client: Client and registry wired through
        the real FastAPI route.
    :returns: None.
    """
    assert routed_tunnel_client.registry.online_runner_ids() == [_RUNNER_ID]
    response = await routed_tunnel_client.client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ws_tunnel_status_reports_registration(app: FastAPI) -> None:
    """Runner status flips online after tunnel registration.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :returns: None.
    """
    registry = app.state.tunnel_registry

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://server",
    ) as client:
        offline = await client.get(f"/v1/runners/{_RUNNER_ID}/status")

        communicator = await _connect_route(app, _TUNNEL_PATH)
        await _send_hello(communicator, registry)
        try:
            online = await client.get(f"/v1/runners/{_RUNNER_ID}/status")
        finally:
            await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
            with contextlib.suppress(asyncio.TimeoutError):
                await communicator.wait(timeout=1.0)

    assert offline.json() == {"runner_id": _RUNNER_ID, "online": False}
    assert online.json() == {"runner_id": _RUNNER_ID, "online": True}


async def test_ws_tunnel_list_runners_reports_online_harnesses(app: FastAPI) -> None:
    """Runner list exposes live runners and advertised harnesses.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :returns: None.
    """
    registry = app.state.tunnel_registry

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://server",
    ) as client:
        offline = await client.get("/v1/runners")

        communicator = await _connect_route(app, _TUNNEL_PATH)
        await _send_hello(communicator, registry)
        try:
            online = await client.get("/v1/runners")
        finally:
            await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
            with contextlib.suppress(asyncio.TimeoutError):
                await communicator.wait(timeout=1.0)

    assert offline.json() == {"data": []}
    assert online.json() == {
        "data": [
            {
                "runner_id": _RUNNER_ID,
                "online": True,
                "harnesses": ["claude-sdk"],
            }
        ]
    }


async def test_ws_tunnel_rejects_token_runner_id_mismatch(app: FastAPI) -> None:
    """Token-bound tunnels cannot claim arbitrary runner ids.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :returns: None.
    """
    registry = app.state.tunnel_registry
    headers = [(RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"), b"bind-token")]
    communicator = ApplicationCommunicator(
        app,
        _websocket_scope(_TUNNEL_PATH, headers=headers),
    )

    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=1.0)
    closed = await communicator.receive_output(timeout=1.0)

    assert accepted["type"] == "websocket.accept"
    assert closed == {
        "type": "websocket.close",
        "code": 4004,
        "reason": "runner_id does not match tunnel token",
    }
    assert registry.online_runner_ids() == []


async def test_ws_tunnel_accepts_ipv4_mapped_loopback_client(app: FastAPI) -> None:
    """IPv4-mapped IPv6 loopback clients are local runner tunnels.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :returns: None.
    """
    registry = app.state.tunnel_registry
    communicator = await _connect_route(
        app,
        _TUNNEL_PATH,
        client_host="::ffff:127.0.0.1",
    )

    await _send_hello(communicator, registry)
    try:
        assert registry.online_runner_ids() == [_RUNNER_ID]
    finally:
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


@pytest.mark.parametrize("client_host", ["10.1.2.3", "::ffff:10.1.2.3"])
async def test_ws_tunnel_requires_token_for_non_loopback_client(
    app: FastAPI, client_host: str
) -> None:
    """Remote runner tunnels must present a binding token.

    Both bare IPv4 and IPv4-mapped IPv6 non-loopback peers are
    parametrized so the mapped-address normalization in
    ``_is_loopback_websocket_client`` cannot regress into
    allowing arbitrary mapped peers without a token.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :param client_host: ASGI client host under test.
    :returns: None.
    """
    registry = app.state.tunnel_registry
    communicator = ApplicationCommunicator(
        app,
        _websocket_scope(_TUNNEL_PATH, client_host=client_host),
    )

    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=1.0)
    closed = await communicator.receive_output(timeout=1.0)

    assert accepted["type"] == "websocket.accept"
    assert closed == {
        "type": "websocket.close",
        "code": 4004,
        "reason": "runner tunnel token is required",
    }
    assert registry.online_runner_ids() == []


async def test_ws_tunnel_allowlist_requires_token_for_remote_client() -> None:
    """Remote clients must present a token when the server has an allow-list.

    Uses a non-loopback client host because loopback clients bypass
    the allow-list (they are trusted local connections).

    :returns: None.
    """
    route_app = _tunnel_route_app(
        allowed_tunnel_tokens=frozenset({"current-token"}),
    )
    communicator = ApplicationCommunicator(
        route_app.app,
        _websocket_scope(_TUNNEL_PATH, client_host="10.0.0.1"),
    )

    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=1.0)
    closed = await communicator.receive_output(timeout=1.0)

    # Remote client with no token → rejected.
    assert accepted["type"] == "websocket.accept"
    assert closed == {
        "type": "websocket.close",
        "code": 4004,
        "reason": "runner tunnel token is required",
    }
    assert route_app.registry.online_runner_ids() == []


async def test_ws_tunnel_allowlist_rejects_stale_remote_token() -> None:
    """A stale runner token from a remote client is rejected.

    Uses a non-loopback client host because loopback clients bypass
    the allow-list entirely.

    :returns: None.
    """
    route_app = _tunnel_route_app(
        allowed_tunnel_tokens=frozenset({"current-token"}),
    )
    stale_token = "stale-token"
    stale_runner_id = token_bound_runner_id(stale_token)
    communicator = ApplicationCommunicator(
        route_app.app,
        _websocket_scope(
            f"/v1/runners/{stale_runner_id}/tunnel",
            headers=[
                (
                    RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"),
                    stale_token.encode("ascii"),
                )
            ],
            client_host="10.0.0.1",
        ),
    )

    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=1.0)
    closed = await communicator.receive_output(timeout=1.0)

    # Remote client with stale token → rejected.
    assert accepted["type"] == "websocket.accept"
    assert closed == {
        "type": "websocket.close",
        "code": 4004,
        "reason": "runner tunnel token is not authorized",
    }
    assert route_app.registry.online_runner_ids() == []


async def test_ws_tunnel_allowlist_accepts_current_server_token() -> None:
    """A stable local runner id with the current server token can register.

    Uses a non-loopback client to prove the allow-list path works
    independently of the loopback bypass.

    :returns: None.
    """
    token = "current-token"
    runner_id = "runner_local_stable"
    route_app = _tunnel_route_app(allowed_tunnel_tokens=frozenset({token}))
    communicator = await _connect_route(
        route_app.app,
        f"/v1/runners/{runner_id}/tunnel",
        headers=[
            (
                RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"),
                token.encode("ascii"),
            )
        ],
        client_host="10.0.0.1",
    )

    await _send_hello(communicator, route_app.registry, runner_id=runner_id)
    try:
        # Remote client with valid token → accepted.
        assert route_app.registry.online_runner_ids() == [runner_id]
    finally:
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


async def test_ws_tunnel_loopback_bypasses_allowlist() -> None:
    """Loopback clients skip the token allow-list entirely.

    This is the ``run --server http://127.0.0.1:...`` scenario: the
    server has an allow-list for its own runner, but a second loopback
    runner with a different binding token should still connect. The
    runner_id is derived from the binding token via
    ``token_bound_runner_id``.

    :returns: None.
    """
    external_token = "external-runner-token"
    external_runner_id = token_bound_runner_id(external_token)
    route_app = _tunnel_route_app(
        allowed_tunnel_tokens=frozenset({"server-own-token"}),
    )
    communicator = await _connect_route(
        route_app.app,
        f"/v1/runners/{external_runner_id}/tunnel",
        headers=[
            (
                RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"),
                external_token.encode("ascii"),
            )
        ],
        client_host="127.0.0.1",
    )

    await _send_hello(communicator, route_app.registry, runner_id=external_runner_id)
    try:
        # Loopback client with a token NOT in the allow-list → still
        # accepted because loopback bypasses the allow-list. If this
        # fails, the loopback bypass in the tunnel route is broken
        # and `run --server` against a local server won't work.
        assert route_app.registry.online_runner_ids() == [external_runner_id]
    finally:
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


async def test_ws_tunnel_accepts_token_bound_runner_id(app: FastAPI) -> None:
    """Token-bound tunnels can register their derived runner id.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :returns: None.
    """
    registry = app.state.tunnel_registry
    token = "bind-token"
    runner_id = token_bound_runner_id(token)
    path = f"/v1/runners/{runner_id}/tunnel"
    communicator = await _connect_route(
        app,
        path,
        headers=[(RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"), token.encode("ascii"))],
    )

    await _send_hello(communicator, registry, runner_id=runner_id)
    try:
        assert registry.online_runner_ids() == [runner_id]
    finally:
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


async def test_ws_tunnel_accepts_multiple_remote_runner_ids(app: FastAPI) -> None:
    """Concurrent remote runner ids register independently.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :returns: None.
    """
    registry = app.state.tunnel_registry
    first_token = "bind-token-one"
    first_runner_id = token_bound_runner_id(first_token)
    first = await _connect_route(
        app,
        f"/v1/runners/{first_runner_id}/tunnel",
        headers=[
            (
                RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"),
                first_token.encode("ascii"),
            )
        ],
    )
    await _send_hello(first, registry, runner_id=first_runner_id)

    second_token = "bind-token-two"
    second_runner_id = token_bound_runner_id(second_token)
    second = await _connect_route(
        app,
        f"/v1/runners/{second_runner_id}/tunnel",
        headers=[
            (
                RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"),
                second_token.encode("ascii"),
            )
        ],
    )
    await _send_hello(second, registry, runner_id=second_runner_id)
    try:
        assert registry.online_runner_ids() == [first_runner_id, second_runner_id]
    finally:
        await second.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await second.wait(timeout=1.0)
        await first.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await first.wait(timeout=1.0)


@pytest.mark.parametrize(
    "bad_frame",
    [
        pytest.param({"type": "websocket.receive", "text": "not even json"}, id="bad-json"),
        pytest.param({"type": "websocket.receive", "bytes": b"\xff\xfe"}, id="binary"),
        pytest.param(
            {
                "type": "websocket.receive",
                "text": '{"kind":"response.head","id":"r","status":200,"headers":123}',
            },
            id="bad-optional-field",
        ),
    ],
)
async def test_ws_tunnel_route_survives_malformed_frame(
    app: FastAPI,
    bad_frame: dict[str, object],
) -> None:
    """One bad frame must not deregister the runner or abort routing.

    :param app: Production FastAPI app from ``tests.server`` fixtures.
    :param bad_frame: ASGI WebSocket input that should be dropped.
    :returns: None.
    """
    registry = app.state.tunnel_registry

    communicator = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello(communicator, registry)

    runner_app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    route_task = asyncio.create_task(
        _route_requests_to_runner(communicator, runner_app),
        name="route-after-malformed",
    )

    try:
        await communicator.send_input(bad_frame)

        async with httpx.AsyncClient(
            transport=WSTunnelTransport(registry, _RUNNER_ID),
            base_url="http://runner",
        ) as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert registry.online_runner_ids() == [_RUNNER_ID]
    finally:
        route_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await route_task
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


async def test_ws_tunnel_route_is_not_double_prefixed(app: FastAPI) -> None:
    """The tunnel route is accepted at one ``/v1`` prefix, not two.

    :param app: Production FastAPI app from ``tests.server``
        fixtures.
    :returns: None.
    """
    registry = app.state.tunnel_registry
    bad_path = f"/v1{_TUNNEL_PATH}"
    communicator = ApplicationCommunicator(app, _websocket_scope(bad_path))

    await communicator.send_input({"type": "websocket.connect"})
    rejected = await communicator.receive_output(timeout=1.0)

    assert rejected["type"] == "websocket.close"
    assert registry.online_runner_ids() == []


# ── runner tunnel must fail closed on null owner ──
#
# These tests drive the REAL WS route (not register_test_runner, which
# injects an owner directly and so never exercised the handshake bug).
# On an auth-enabled server with no token allow-list — the standard
# deployed posture (see cli.py) — an unauthenticated non-loopback peer
# could derive a valid runner id from an attacker-chosen token and
# register with owner=None, bypassing the binding ownership check and the
# listing filter (both skip enforcement when owner is None).


@pytest.mark.parametrize("client_host", ["203.0.113.7", "::ffff:203.0.113.7"])
async def test_ws_tunnel_rejects_unauthenticated_non_loopback_peer(
    client_host: str,
) -> None:
    """An unauthenticated non-loopback peer cannot register.

    With auth enabled and no allow-list, a peer presents an
    attacker-chosen token (which derives a valid path runner id and
    clears the token-binding gate) but NO authenticated identity. The
    handshake must be refused with 4004 *before* ``accept()`` — the
    runner must never enter the registry with ``owner=None``.

    Reverting the fix turns this red: the route would accept the
    upgrade (first output ``websocket.accept``, not ``websocket.close``)
    and, after a hello frame, register an owner-less runner that every
    tenant could see and bind. Both an IPv4 and an IPv4-mapped IPv6
    peer are parametrized so the loopback normalization cannot regress
    into treating a mapped non-loopback address as local.

    :param client_host: Non-loopback ASGI client host under test.
    :returns: None.
    """
    route_app = _tunnel_route_app(auth_provider=_CredentialHeaderAuthProvider())

    attacker_token = "attacker-chosen-token"
    derived_runner_id = token_bound_runner_id(attacker_token)
    communicator = ApplicationCommunicator(
        route_app.app,
        _websocket_scope(
            f"/v1/runners/{derived_runner_id}/tunnel",
            headers=[
                (
                    RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"),
                    attacker_token.encode("ascii"),
                )
            ],
            client_host=client_host,
        ),
    )

    await communicator.send_input({"type": "websocket.connect"})
    closed = await communicator.receive_output(timeout=1.0)

    # Refused before accept (no acceptance oracle): the very first
    # output is the close frame. If the fix is reverted this is a
    # ``websocket.accept`` instead and the equality fails.
    assert closed == {
        "type": "websocket.close",
        "code": 4004,
        "reason": "unauthenticated",
    }
    # Nothing registered → the owner-less runner is neither visible
    # nor bindable by any tenant.
    assert route_app.registry.online_runner_ids() == []


async def test_ws_tunnel_registers_authenticated_non_loopback_owner() -> None:
    """An authenticated remote runner registers under its owner.

    The fix must not break the legitimate ``run --server`` flow: a
    remote runner that presents both a binding token (deriving its
    runner id) and a valid identity (here the credential header
    standing in for an OAuth cookie / Bearer) must register, and the
    registry must record the authenticated owner so a later ownership check
    can forbid a different user from binding to it.

    :returns: None.
    """
    route_app = _tunnel_route_app(auth_provider=_CredentialHeaderAuthProvider())

    token = "alice-runner-token"
    runner_id = token_bound_runner_id(token)
    communicator = await _connect_route(
        route_app.app,
        f"/v1/runners/{runner_id}/tunnel",
        headers=[
            (RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"), token.encode("ascii")),
            (b"x-test-user", b"alice@example.com"),
        ],
        client_host="203.0.113.7",
    )

    await _send_hello(communicator, route_app.registry, runner_id=runner_id)
    try:
        assert route_app.registry.online_runner_ids() == [runner_id]
        # Owner recorded as the authenticated caller (not None, not
        # "local") — this is what the binding ownership check enforces on.
        assert route_app.registry.runner_owner(runner_id) == "alice@example.com"
    finally:
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


async def test_ws_tunnel_managed_runner_resolves_owner_from_binding_token() -> None:
    """A managed-sandbox runner registers under its launch owner.

    Server-managed sandboxes authenticate with a server-minted binding
    token, not an OIDC cookie/Bearer, so ``auth_provider.get_user_id``
    returns ``None`` for the runner handshake. Rather than rejecting it
    (which would make server-managed sandboxes impossible on an
    auth-enabled server), the route resolves the owner the server
    recorded for this runner at launch via ``resolve_managed_runner_owner``
    — the runner-side analog of the host tunnel's ``resolve_launch_token``.

    The peer still had to present the real binding token to clear the
    token-binding gate (``token_bound_runner_id(token) == runner_id``),
    so this path is unreachable with an attacker-chosen token mapped to a
    victim's runner id.

    Reverting the fix turns this red: the route refuses the handshake
    (``_connect_route`` asserts ``websocket.accept`` and would instead see
    a ``websocket.close``).

    :returns: None.
    """
    token = "managed-runner-binding-token"
    runner_id = token_bound_runner_id(token)
    route_app = _tunnel_route_app(
        auth_provider=_CredentialHeaderAuthProvider(),
        resolve_managed_runner_owner=(
            lambda rid: "owner@example.com" if rid == runner_id else None
        ),
    )
    communicator = await _connect_route(
        route_app.app,
        f"/v1/runners/{runner_id}/tunnel",
        headers=[
            (RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"), token.encode("ascii")),
        ],
        client_host="203.0.113.7",
    )

    await _send_hello(communicator, route_app.registry, runner_id=runner_id)
    try:
        assert route_app.registry.online_runner_ids() == [runner_id]
        # Registered under the launch owner the resolver returned — not
        # rejected, and not the owner-less registration the gate forbids.
        assert route_app.registry.runner_owner(runner_id) == "owner@example.com"
    finally:
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


async def test_ws_tunnel_managed_resolver_none_still_rejects() -> None:
    """Wiring the managed resolver must not weaken the fail-closed gate.

    A non-loopback peer with a token but no authenticated identity AND no
    managed-launch record (the resolver returns ``None``) is still refused
    with 4004 *before* ``accept()`` — never registered owner-less. This
    locks in that the new resolver path only rescues genuine
    server-launched runners, not an attacker-chosen token.

    :returns: None.
    """
    route_app = _tunnel_route_app(
        auth_provider=_CredentialHeaderAuthProvider(),
        resolve_managed_runner_owner=lambda rid: None,
    )

    attacker_token = "attacker-chosen-token"
    derived_runner_id = token_bound_runner_id(attacker_token)
    communicator = ApplicationCommunicator(
        route_app.app,
        _websocket_scope(
            f"/v1/runners/{derived_runner_id}/tunnel",
            headers=[
                (
                    RUNNER_TUNNEL_TOKEN_HEADER.lower().encode("ascii"),
                    attacker_token.encode("ascii"),
                )
            ],
            client_host="203.0.113.7",
        ),
    )

    await communicator.send_input({"type": "websocket.connect"})
    closed = await communicator.receive_output(timeout=1.0)
    assert closed == {
        "type": "websocket.close",
        "code": 4004,
        "reason": "unauthenticated",
    }
    assert route_app.registry.online_runner_ids() == []


async def test_ws_tunnel_loopback_unauthenticated_registers_as_local() -> None:
    """Auth-enabled server still accepts the local loopback runner.

    ``omnigent server`` starts an unauthenticated runner that connects
    over loopback with no credentials. The fail-closed gate
    applies only to non-loopback peers, so this runner must still
    register — owned by the reserved single-user identity, not rejected
    and not left owner=None.

    :returns: None.
    """
    route_app = _tunnel_route_app(auth_provider=_CredentialHeaderAuthProvider())

    communicator = await _connect_route(
        route_app.app,
        _TUNNEL_PATH,
        client_host="127.0.0.1",
    )

    await _send_hello(communicator, route_app.registry)
    try:
        assert route_app.registry.online_runner_ids() == [_RUNNER_ID]
        # Loopback + no credential → reserved local identity, so
        # single-user ownership checks remain coherent.
        assert route_app.registry.runner_owner(_RUNNER_ID) == RESERVED_USER_LOCAL
    finally:
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.TimeoutError):
            await communicator.wait(timeout=1.0)


# ── Managed-runner token mint endpoint (POST /v1/runners/{id}/token) ──


class _MintingAuthProvider(_CredentialHeaderAuthProvider):
    """OIDC/accounts-style provider that also mints runner owner tokens.

    Models the deployed contract where ``mint_runner_token`` returns a
    bearer. The real ``UnifiedAuthProvider`` signs a JWT; here a
    deterministic sentinel exercises the route without JWT machinery —
    the token round-trip itself is covered in
    ``tests/server/test_accounts.py``.
    """

    def mint_runner_token(self, user_id: str, ttl_seconds: int) -> str | None:
        """Return a deterministic sentinel bearer for *user_id*."""
        return f"minted-owner-token:{user_id}:{ttl_seconds}"


def _mint_route_app(
    *,
    auth_provider: AuthProvider | None,
    resolve_managed_runner_owner: Callable[[str], str | None] | None,
) -> FastAPI:
    """Tunnel-route app with the ``OmnigentError`` -> HTTP handler installed.

    The bare :func:`_tunnel_route_app` omits ``create_app``'s exception
    handler, so the mint endpoint's ``OmnigentError`` would surface as a
    raw 500. Install the same mapping here so the tests assert the real
    401 / 400 statuses the endpoint intends.

    :param auth_provider: Auth provider wired into the route.
    :param resolve_managed_runner_owner: ``runner_id -> owner`` resolver.
    :returns: The FastAPI app (error handler installed).
    """
    app = _tunnel_route_app(
        auth_provider=auth_provider,
        resolve_managed_runner_owner=resolve_managed_runner_owner,
    ).app

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        """Map the application error to its HTTP status (mirrors create_app)."""
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return app


async def _post_mint_token(
    app: FastAPI,
    runner_id: str,
    *,
    token: str | None,
) -> httpx.Response:
    """POST the mint endpoint with an optional binding-token header.

    :param app: The tunnel-route app under test.
    :param runner_id: Path runner id.
    :param token: Binding token for the ``X-Omnigent-Runner-Tunnel-Token``
        header, or ``None`` to omit it.
    :returns: The HTTP response.
    """
    headers = {} if token is None else {RUNNER_TUNNEL_TOKEN_HEADER: token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://server",
    ) as client:
        return await client.post(f"/v1/runners/{runner_id}/token", headers=headers)


async def test_mint_token_endpoint_returns_owner_bearer_for_valid_binding_token() -> None:
    """A valid binding token mints an owner bearer scoped to the launch owner.

    The HTTP analog of the tunnel handshake: the managed runner presents
    its binding token and receives a short-lived owner JWT (here the
    provider's sentinel) plus an expiry, which it then uses on its HTTP
    callbacks.

    :returns: None.
    """
    token = "managed-runner-binding-token"
    runner_id = token_bound_runner_id(token)
    app = _mint_route_app(
        auth_provider=_MintingAuthProvider(),
        resolve_managed_runner_owner=(
            lambda rid: "owner@example.com" if rid == runner_id else None
        ),
    )

    response = await _post_mint_token(app, runner_id, token=token)

    assert response.status_code == 200
    body = response.json()
    assert body["token"] == "minted-owner-token:owner@example.com:1800"
    assert isinstance(body["expires_at"], int)
    assert body["expires_at"] > 0


async def test_mint_token_endpoint_rejects_unrecognized_token() -> None:
    """A token with no managed-launch record is refused (fail closed).

    An attacker-chosen token clears the SHA-256 gate for its *own*
    runner_id, but that id has no bound conversation, so the resolver
    returns ``None`` and minting is refused — the same posture as the
    tunnel handshake.

    :returns: None.
    """
    attacker_token = "attacker-chosen-token"
    runner_id = token_bound_runner_id(attacker_token)
    app = _mint_route_app(
        auth_provider=_MintingAuthProvider(),
        resolve_managed_runner_owner=lambda _rid: None,
    )

    response = await _post_mint_token(app, runner_id, token=attacker_token)

    assert response.status_code == 401


async def test_mint_token_endpoint_rejects_runner_id_mismatch() -> None:
    """A token that doesn't hash to the path runner_id is refused.

    The SHA-256 binding gate runs before any owner lookup, so a token
    that maps to a different runner_id cannot mint for the path id even
    if that id has an owner.

    :returns: None.
    """
    app = _mint_route_app(
        auth_provider=_MintingAuthProvider(),
        resolve_managed_runner_owner=lambda _rid: "owner@example.com",
    )

    response = await _post_mint_token(
        app, "runner_token_does_not_match", token="some-binding-token"
    )

    assert response.status_code == 401


async def test_mint_token_endpoint_missing_binding_token_rejected() -> None:
    """A request without the binding-token header is refused.

    :returns: None.
    """
    runner_id = token_bound_runner_id("whatever")
    app = _mint_route_app(
        auth_provider=_MintingAuthProvider(),
        resolve_managed_runner_owner=lambda _rid: "owner@example.com",
    )

    response = await _post_mint_token(app, runner_id, token=None)

    assert response.status_code == 401


async def test_mint_token_endpoint_header_mode_unsupported_returns_400() -> None:
    """When the provider can't mint (header/proxy mode), the endpoint 400s.

    The binding token is valid and the owner resolves, but header/proxy
    identity can't be minted server-side (``mint_runner_token`` returns
    ``None``) — a clear 400, not a 401.

    :returns: None.
    """
    token = "managed-runner-binding-token"
    runner_id = token_bound_runner_id(token)
    app = _mint_route_app(
        # Base provider: mint_runner_token uses the ABC default (None).
        auth_provider=_CredentialHeaderAuthProvider(),
        resolve_managed_runner_owner=(
            lambda rid: "owner@example.com" if rid == runner_id else None
        ),
    )

    response = await _post_mint_token(app, runner_id, token=token)

    assert response.status_code == 400


# ── Regression: receive-side tunnel disconnect cleanup (e2a3bda) ──
#
# These tests exercise the real FastAPI WS route (not the registry in
# isolation) to lock in the receive-side disconnect cleanup that
# landed in e2a3bda. The fix added explicit ``registry.deregister``
# calls in the ``except WebSocketDisconnect`` and the new
# ``except asyncio.CancelledError`` blocks of ``runner_tunnel.py`` so
# that the exact session is retired and in-flight requests are woken
# when the receive side drops or the route handler is cancelled.


async def _wait_for_in_flight(session: object, *, timeout_s: float = 1.0) -> None:
    """Poll until ``session.in_flight`` is non-empty.

    :param session: The runner session whose in-flight map should
        receive a request.
    :param timeout_s: Max seconds to wait before raising.
    :returns: None.
    :raises AssertionError: If no request opens in time.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if session.in_flight:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no request opened in_flight within timeout")


async def _wait_until(predicate: Callable[[], object], *, timeout_s: float = 1.0) -> None:
    """Poll a synchronous predicate until it becomes truthy.

    :param predicate: Zero-argument callable returning a truthy
        value when the wait should stop.
    :param timeout_s: Max seconds to wait before raising.
    :returns: None.
    :raises AssertionError: If the predicate never becomes true.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("predicate did not become true before timeout")


async def test_ws_tunnel_route_cleans_up_on_receive_side_disconnect() -> None:
    """Receive-side WebSocketDisconnect deregisters the session and fails pending requests.

    Exercises the real FastAPI WS route: a runner connects, registers,
    a transport request opens and blocks waiting for the response head,
    then the receive side raises ``WebSocketDisconnect`` (the runner
    dropped). The route handler must retire the exact session and wake
    the pending request within its own bound — not relying on an
    external client timeout.

    The fix in e2a3bda added explicit cleanup in the
    ``except WebSocketDisconnect`` block of ``runner_tunnel.py`` so the
    route handler's outer catch path also retires the session and fires
    ``on_runner_disconnect``, rather than relying solely on the inner
    helper-task wait's ``finally`` block.

    :returns: None.
    """
    route_app = _tunnel_route_app()
    registry = route_app.registry
    runner_id = _RUNNER_ID

    communicator = await _connect_route(route_app.app, _TUNNEL_PATH)
    await _send_hello(communicator, registry, runner_id=runner_id)

    session = registry.get(runner_id)
    assert session is not None, "runner must register after hello"
    assert session.in_flight == {}

    # Open a transport request that will hang on the response head.
    # The transport opens reassembly state in the registry, enqueues a
    # request frame on the session's outbound queue, then blocks on
    # ``state.head_future`` until the runner sends back a head frame.
    transport = WSTunnelTransport(registry, runner_id)
    request_task = asyncio.create_task(
        transport.handle_async_request(httpx.Request("GET", "http://runner/health")),
        name="regression-disconnect-pending-request",
    )
    await asyncio.wait_for(
        _wait_for_in_flight(session, timeout_s=2.0),
        timeout=2.0,
    )
    assert len(session.in_flight) == 1
    req_id = next(iter(session.in_flight))
    assert not request_task.done()

    # Make the receive side raise WebSocketDisconnect by feeding a
    # disconnect event to the ApplicationCommunicator. The route's
    # _receive_task will raise WebSocketDisconnect, which propagates
    # through the helper-task wait and triggers cleanup.
    await communicator.send_input(
        {"type": "websocket.disconnect", "code": 1006, "reason": "abnormal"}
    )

    # Await route-handler cleanup with our own bound (not an external
    # client timeout). The ApplicationCommunicator's ``wait`` returns
    # only after the route handler's task completes, which includes
    # the inner finally deregister and the outer except handling.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(communicator.wait(timeout=2.0), timeout=2.0)

    # Assertion 6: the exact session is deregistered.
    assert registry.get(runner_id) is None, (
        "session must be deregistered after receive-side disconnect; "
        f"registry still contains: {registry.online_runner_ids()!r}"
    )

    # Assertion 7: the pending request must have failed with a
    # tunnel-closed / connection error within the internal bound.
    # The deregister path sets the head_future's exception to
    # ``ConnectionError("tunnel closed before request completed")``,
    # which the transport re-raises.
    with pytest.raises((ConnectionError, httpx.ConnectError)):
        await asyncio.wait_for(request_task, timeout=2.0)
    assert request_task.done(), "request task must have completed"

    # Assertion 8: no request/channel bookkeeping remains. The
    # session was already removed from the registry, so the in-flight
    # map is detached from the registry; the deregister path also
    # cleared the session's in_flight before removing it.
    assert req_id not in session.in_flight, (
        f"req_id {req_id!r} leaked in session.in_flight after deregister; "
        f"remaining: {list(session.in_flight)!r}"
    )
    assert session.ws_channels == {}, "ws_channels must be empty after deregister"

    # Assertion 9: no asynchronous task or unresolved future leaked.
    # The head_future was resolved with an exception by the deregister
    # path; the body_queue was sentinel-pushed. The route handler's
    # helper tasks were cancelled and gathered in the inner finally.
    # The route handler's own task has completed (communicator.wait
    # returned). Verify by checking the request's state directly.
    # (We no longer hold a reference to the RequestState — it was
    # local to the transport — so assert via the request_task outcome.)


async def test_ws_tunnel_route_replacement_generation_preserves_new_session() -> None:
    """A replacement tunnel survives the old handler's cleanup.

    Newest-wins (RUNNER.md §2): when a new tunnel registers under the
    same runner id, the old session is retired and the new one
    replaces it. The old route handler's eventual disconnect cleanup
    must use the identity-safe ``deregister(runner_id, session)`` so
    it does not remove the replacement.

    This exercises the real WS route (not just ``TunnelRegistry`` in
    isolation) so the route handler's ``registry.deregister(runner_id,
    session)`` call site is verified end-to-end. The broad
    identity-safe registry behavior is already covered in
    ``tests/runner/transports/ws_tunnel/test_registry.py::test_stale_deregister_does_not_remove_newest_session``;
    this test only adds the route-level wiring check.

    :returns: None.
    """
    route_app = _tunnel_route_app()
    registry = route_app.registry
    runner_id = _RUNNER_ID

    # Tunnel A registers via the route.
    communicator_a = await _connect_route(route_app.app, _TUNNEL_PATH)
    await _send_hello(communicator_a, registry, runner_id=runner_id)
    session_a = registry.get(runner_id)
    assert session_a is not None

    # Tunnel B replaces A (newest-wins). The registry's register()
    # call aborts session_a's in-flight and retires its sender; the
    # route handler for A is still running but its session is stale.
    # ``_wait_until_registered`` would return immediately because a
    # session is already registered — poll for the replacement
    # instead so we observe the new generation.
    old_session = session_a
    communicator_b = await _connect_route(route_app.app, _TUNNEL_PATH)
    await communicator_b.send_input(
        {
            "type": "websocket.receive",
            "text": encode_frame(
                HelloFrame(
                    runner_version="0.1.0-test",
                    frame_protocol_version=1,
                    harnesses=["claude-sdk"],
                    envs=["os_sandbox"],
                )
            ),
        },
    )
    await asyncio.wait_for(
        _wait_until(
            lambda: (
                registry.get(runner_id) is not None and registry.get(runner_id) is not old_session
            ),
            timeout_s=2.0,
        ),
        timeout=2.0,
    )
    session_b = registry.get(runner_id)
    assert session_b is not None
    assert session_b is not session_a, "register must retire the old session"

    # The runner dials in under the same id again — its runner is
    # tunnel B, not A. Sending the disconnect event for A's
    # communicator triggers A's route handler cleanup. The cleanup
    # must use the identity-safe ``deregister(runner_id, session_a)``
    # so it removes session_a (already gone from the registry) and
    # leaves session_b intact.
    await communicator_a.send_input({"type": "websocket.disconnect", "code": 1000})
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(communicator_a.wait(timeout=2.0), timeout=2.0)

    # Tunnel B remains registered and the registry points to it.
    assert registry.get(runner_id) is session_b, (
        "old handler's cleanup must not remove the replacement session"
    )
    assert registry.online_runner_ids() == [runner_id]

    # Clean up tunnel B.
    await communicator_b.send_input({"type": "websocket.disconnect", "code": 1000})
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(communicator_b.wait(timeout=2.0), timeout=2.0)
    assert registry.get(runner_id) is None


async def test_ws_tunnel_route_deregisters_on_task_cancellation() -> None:
    """Cancelling the route handler retires the session and re-raises CancelledError.

    The fix in e2a3bda added a dedicated ``except asyncio.CancelledError``
    block to ``runner_tunnel.py`` that calls the identity-safe
    ``registry.deregister(runner_id, session)`` and re-raises the
    CancelledError.

    The critical scenario is cancellation *after* registration but
    *before* the inner helper-task ``try/finally`` runs — e.g. the
    route handler is cancelled while awaiting the ``on_runner_connect``
    callback. In that window the inner ``finally`` never executes, so
    the dedicated ``except asyncio.CancelledError`` is the only path
    that can retire the session. Pre-fix there was no such handler;
    ``asyncio.CancelledError`` is a ``BaseException`` (not ``Exception``)
    so the generic ``except Exception`` didn't catch it either — the
    cancellation propagated to the caller with the session left
    registered (a zombie).

    :returns: None.
    """
    connect_started = asyncio.Event()
    connect_proceed = asyncio.Event()

    async def _blocking_on_connect(runner_id: str) -> None:
        """Block the route handler in ``on_runner_connect`` until cancelled.

        The route handler registers the session, then awaits this
        callback. By blocking here we hold the handler between
        registration and the inner ``try/finally`` so cancellation
        skips the inner cleanup.
        """
        connect_started.set()
        await connect_proceed.wait()

    # Build the tunnel route app with the blocking on_connect hook.
    registry = TunnelRegistry()
    app = FastAPI()
    app.include_router(
        create_runner_tunnel_router(
            registry,
            on_runner_connect=_blocking_on_connect,
        ),
        prefix="/v1",
    )
    runner_id = _RUNNER_ID

    communicator = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello(communicator, registry, runner_id=runner_id)

    # Wait for the session to be registered and the on_connect
    # callback to start blocking.
    await asyncio.wait_for(connect_started.wait(), timeout=2.0)
    session = registry.get(runner_id)
    assert session is not None, "session must be registered after hello"

    # Open a transport request that hangs on the response head. This
    # verifies the pending request is also woken on cancellation.
    transport = WSTunnelTransport(registry, runner_id)
    request_task = asyncio.create_task(
        transport.handle_async_request(httpx.Request("GET", "http://runner/health")),
        name="regression-cancel-pending-request",
    )
    await asyncio.wait_for(
        _wait_for_in_flight(session, timeout_s=2.0),
        timeout=2.0,
    )
    assert len(session.in_flight) == 1

    # Cancel the route handler's task. The on_runner_connect callback
    # is cancelled (it runs in the same task), the CancelledError
    # propagates out of the callback's try/except (which catches
    # Exception but NOT CancelledError), and the inner try/finally
    # is never entered.
    future = communicator.future
    assert future is not None
    future.cancel()
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(communicator.wait(timeout=2.0), timeout=2.0)

    # Release the on_connect callback so any lingering reference is
    # unblocked (defensive; the task is already cancelled).
    connect_proceed.set()

    # The session must be retired. Pre-fix this fails: the session
    # leaks because neither the inner finally (never entered) nor
    # any except handler caught the CancelledError.
    assert registry.get(runner_id) is None, (
        "session must be deregistered after task cancellation; "
        f"registry still contains: {registry.online_runner_ids()!r}"
    )

    # The pending request must have failed with a tunnel-closed error.
    with pytest.raises((ConnectionError, httpx.ConnectError, asyncio.CancelledError)):
        await asyncio.wait_for(request_task, timeout=2.0)
    assert request_task.done()
