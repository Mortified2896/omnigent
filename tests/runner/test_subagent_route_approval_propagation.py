"""Focused tests for the parent-route-approval propagation helper.

``sys_session_send`` creates the child session via
``POST /v1/sessions``. The server defaults ``route_approval_enabled``
to the global ``OMNIGENT_ROUTE_APPROVAL_GATE`` env var, so a parent
that explicitly toggled the flag (e.g. a fixed-route orchestrator that
turned the model-routing recommender off for the V1 smoke test) would
otherwise spawn a child that re-enables it. The fix is a single
GET against the parent session, a pass-through on the child create
body, and a non-fatal fallback when the lookup fails.

The helper is small and the surface area narrow — assert the three
behaviours: pass-through when the parent has an explicit value, ``None``
when the lookup fails, ``None`` when the inputs are absent.
"""

from __future__ import annotations

import asyncio

import httpx

from omnigent.runner.tool_dispatch import _fetch_parent_route_approval_flag


class _MockAsyncClient:
    """Minimal ``httpx.AsyncClient``-shape stand-in.

    Captures the request so tests can assert on it, returns a
    pre-canned response, or simulates a transport failure.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: dict[str, object] | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, float]] = []
        self._status_code = status_code
        self._body = body
        self._raise_exc = raise_exc

    async def get(self, url: str, *, timeout: float) -> httpx.Response:
        self.calls.append((url, timeout))
        if self._raise_exc is not None:
            raise self._raise_exc
        request = httpx.Request("GET", url)
        return httpx.Response(self._status_code, json=self._body, request=request)


def test_returns_true_when_parent_has_explicit_true() -> None:
    """A parent with ``route_approval_enabled=true`` propagates as ``True``."""
    client = _MockAsyncClient(
        status_code=200,
        body={"route_approval_enabled": True},
    )
    flag = asyncio.run(_fetch_parent_route_approval_flag(client, "conv_parent123"))
    assert flag is True
    assert client.calls == [("/v1/sessions/conv_parent123", 10.0)]


def test_returns_false_when_parent_has_explicit_false() -> None:
    """A parent with ``route_approval_enabled=false`` propagates as ``False``.

    This is the fixed-route V1 case: the parent toggled the gate off and
    the child must inherit it so the model-routing recommender does not
    pop a route-approval card back to the user when the orchestrator
    dispatches the implementation message.
    """
    client = _MockAsyncClient(
        status_code=200,
        body={"route_approval_enabled": False},
    )
    flag = asyncio.run(_fetch_parent_route_approval_flag(client, "conv_parent123"))
    assert flag is False


def test_returns_none_when_parent_has_no_explicit_value() -> None:
    """``None`` when the parent row omits the flag (server-default)."""
    client = _MockAsyncClient(status_code=200, body={"other_field": "x"})
    flag = asyncio.run(_fetch_parent_route_approval_flag(client, "conv_parent123"))
    assert flag is None


def test_returns_none_when_lookup_returns_non_200() -> None:
    """``None`` when the parent GET returns a non-OK status.

    The server might 404/5xx during a transient failure; the dispatch
    path then falls back to the server default (no behavior change for
    unrelated callers).
    """
    client = _MockAsyncClient(status_code=500, body=None)
    flag = asyncio.run(_fetch_parent_route_approval_flag(client, "conv_parent123"))
    assert flag is None


def test_returns_none_when_transport_raises() -> None:
    """``None`` when the transport raises (httpx error, etc.).

    Non-fatal by design — unrelated callers must not break.
    """
    client = _MockAsyncClient(raise_exc=httpx.ConnectError("boom"))
    flag = asyncio.run(_fetch_parent_route_approval_flag(client, "conv_parent123"))
    assert flag is None


def test_returns_none_when_server_client_is_none() -> None:
    """``None`` when no server client is wired (e.g. executor-only path).

    The dispatch contract supports ``server_client=None``; the helper
    short-circuits without attempting a request.
    """
    flag = asyncio.run(_fetch_parent_route_approval_flag(None, "conv_parent123"))
    assert flag is None


def test_returns_none_when_conversation_id_is_none() -> None:
    """``None`` when no parent conversation is in scope."""
    client = _MockAsyncClient(status_code=200, body={"route_approval_enabled": True})
    flag = asyncio.run(_fetch_parent_route_approval_flag(client, None))
    assert flag is None
    # And no GET was issued.
    assert client.calls == []


def test_returns_none_when_response_body_is_not_a_dict() -> None:
    """Defensive: a junk body (e.g. list) does not crash the helper."""
    client = _MockAsyncClient(status_code=200, body=["not", "a", "dict"])
    flag = asyncio.run(_fetch_parent_route_approval_flag(client, "conv_parent123"))
    assert flag is None


def test_returns_none_when_flag_is_non_boolean() -> None:
    """The schema types ``route_approval_enabled`` as bool; defensively
    coerce any unexpected shape to ``None`` so the dispatch path never
    forwards a malformed value into a child session row.
    """
    client = _MockAsyncClient(
        status_code=200,
        body={"route_approval_enabled": "yes"},  # not a bool
    )
    flag = asyncio.run(_fetch_parent_route_approval_flag(client, "conv_parent123"))
    assert flag is None
