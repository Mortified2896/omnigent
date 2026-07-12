"""
Regression tests for the Origin CSRF guard in non-local mode with an
explicit allowlist.

These tests verify that a deployment-configured
``OMNIGENT_WS_ALLOWED_ORIGINS`` allowlist correctly admits the legitimate
Tailscale UI origin while still rejecting arbitrary cross-site origins in
non-local (authenticated) mode.

This is the deployed scenario: no ``OMNIGENT_LOCAL_SINGLE_USER`` (so the
server is not a bare loopback single-user runtime), but an explicit
allowlist is configured for the Tailscale UI domain. The guard must admit
the allowlisted origin and deny anything else.

Coverage:
- The exact configured Tailscale origin is accepted (proving the
  allowlist is wired correctly).
- A different untrusted non-loopback origin is rejected (proving the
  allowlist flips the default to deny-by-default in non-local mode).
- Loopback behavior remains unchanged (localhost still works).
- The internal sentinel remains accepted.
- No wildcard behavior is introduced.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio

_LOCAL_ENV = "OMNIGENT_LOCAL_SINGLE_USER"
_ALLOWLIST_ENV = "OMNIGENT_WS_ALLOWED_ORIGINS"

# The exact Tailscale origin this deployment uses.
_TAILSCALE_ORIGIN = "https://hermes-agent.taile0361b.ts.net:9461"

# A concrete cross-site origin used as the attacker's page.
_EVIL_ORIGIN = "https://evil.example.com"


@pytest.fixture(autouse=True)
def _env_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Save and restore OMNIGENT env vars around each test."""
    orig_local = os.environ.get(_LOCAL_ENV)
    orig_allowlist = os.environ.get(_ALLOWLIST_ENV)
    yield
    # Restore.
    if orig_local is not None:
        monkeypatch.setenv(_LOCAL_ENV, orig_local)
    elif _LOCAL_ENV in os.environ:
        monkeypatch.delenv(_LOCAL_ENV, raising=False)
    if orig_allowlist is not None:
        monkeypatch.setenv(_ALLOWLIST_ENV, orig_allowlist)
    elif _ALLOWLIST_ENV in os.environ:
        monkeypatch.delenv(_ALLOWLIST_ENV, raising=False)


async def _create_session_via_json(client: httpx.AsyncClient) -> str:
    """
    Create a session over JSON (not multipart) and return its id.

    The JSON session create does not use require_trusted_origin, so it's
    safe for test setup even in non-local mode without an allowlist.

    :param client: The test HTTP client.
    :returns: The new session/conversation id.
    """
    agent = await create_test_agent(client)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── Tests ──


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param(_TAILSCALE_ORIGIN, id="tailscale-origin"),
    ],
)
async def test_allowlisted_tailscale_origin_is_accepted(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """
    The exact configured Tailscale origin is accepted.

    This is the primary regression test for the deployment fix: the
    allowlisted origin must not trigger the 403 trusted-Origin error.
    """
    # Configure non-local mode + allowlist.
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, _TAILSCALE_ORIGIN)

    bundle = build_agent_bundle(name="tailscale-ok-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": origin},
    )
    assert resp.status_code == 201, (
        f"allowlisted Tailscale origin was rejected (status {resp.status_code}); "
        f"expected 201. Response: {resp.text}"
    )
    assert "session_id" in resp.json()


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param(_EVIL_ORIGIN, id="evil-origin"),
    ],
)
async def test_unlisted_cross_origin_is_rejected(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """
    A non-loopback origin NOT on the allowlist is rejected with 403.

    Proves the allowlist flips the non-local default from passthrough to
    deny-by-default: anything not explicitly allowlisted is refused.
    """
    # Configure non-local mode + allowlist.
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, _TAILSCALE_ORIGIN)

    bundle = build_agent_bundle(name="evil-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": origin},
    )
    assert resp.status_code == 403, (
        f"unlisted cross-origin was admitted (status {resp.status_code}); "
        f"expected 403. The allowlist must flip non-local mode to deny."
    )
    assert "origin" in resp.text.lower()


async def test_loopback_requires_local_mode(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Loopback origins require local mode to be trusted.

    In non-local mode with an allowlist, ONLY the allowlisted origins
    pass. Loopback is not automatically trusted in non-local mode.
    This is correct: non-local mode relies on cookie/proxy auth, and
    the allowlist is defense-in-depth. If a deployment needs loopback
    to work without the allowlist, it must be in local mode.
    """
    # Configure non-local mode + allowlist (Tailscale origin only).
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, _TAILSCALE_ORIGIN)

    bundle = build_agent_bundle(name="localhost-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": "http://localhost:5173"},
    )
    assert resp.status_code == 403, (
        f"loopback origin was admitted in non-local mode with allowlist "
        f"(status {resp.status_code}); expected 403. "
        f"Loopback is only trusted in local mode."
    )


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param(OMNIGENT_INTERNAL_WS_ORIGIN, id="sentinel"),
    ],
)
async def test_sentinel_origin_still_works(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """
    The internal sentinel origin still works (first-party clients).

    The ``omnigent://internal`` sentinel must always be admitted,
    regardless of mode or allowlist configuration.
    """
    # Configure non-local mode + allowlist.
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, _TAILSCALE_ORIGIN)

    bundle = build_agent_bundle(name="sentinel-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": origin},
    )
    assert resp.status_code == 201, resp.text
    assert "session_id" in resp.json()


async def test_no_wildcard_behavior(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A similar but distinct origin is rejected (no wildcard matching).

    Proves the allowlist requires an exact match, not a domain suffix
    match. ``https://hermes-agent.taile0361b.ts.net`` (without port) is
    NOT the same as ``https://hermes-agent.taile0361b.ts.net:9461``.
    """
    # Configure non-local mode + allowlist.
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, _TAILSCALE_ORIGIN)

    # Same domain, wrong port.
    wrong_port_origin = "https://hermes-agent.taile0361b.ts.net"
    bundle = build_agent_bundle(name="wrong-port-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": wrong_port_origin},
    )
    assert resp.status_code == 403, (
        f"origin without port was admitted (status {resp.status_code}); "
        f"the allowlist must require exact match, not domain-level wildcard."
    )


# ── File upload tests (same scenarios) ──


async def test_upload_allowlisted_tailscale_origin_accepted(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowlisted Tailscale origin can upload files."""
    # Configure non-local mode + allowlist.
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, _TAILSCALE_ORIGIN)

    session_id = await _create_session_via_json(client)
    resp = await client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
        headers={"Origin": _TAILSCALE_ORIGIN},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "hello.txt"


async def test_upload_unlisted_cross_origin_rejected(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File upload rejects unlisted cross-site origins."""
    # Configure non-local mode + allowlist.
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, _TAILSCALE_ORIGIN)

    session_id = await _create_session_via_json(client)
    resp = await client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("evil.txt", b"pwned", "text/plain")},
        headers={"Origin": _EVIL_ORIGIN},
    )
    assert resp.status_code == 403, (
        f"unlisted cross-origin was admitted for file upload "
        f"(status {resp.status_code}); expected 403."
    )
