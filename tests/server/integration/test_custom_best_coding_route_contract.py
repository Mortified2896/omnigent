"""Live OmniRoute catalog → interactive execution route contract.

Pins the fix for the "Value error, unknown native OmniRoute route id"
regression on the canonical interactive execution route
``custom/best-coding``. The live OmniRoute ``/v1/models`` endpoint
exposes ``custom/best-coding`` and ``custom/outcome-scoring`` as
owned_by="combo" rows; the Omnigent catalog layer must surface them
in ``GET /v1/omniroute/combos``, and the session-create validator
must accept ``custom/best-coding`` while still rejecting
``custom/outcome-scoring`` (the M3-only background Task Outcome
evaluator route).

These tests use mocked httpx transports so they never hit the real
OmniRoute endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server import app as server_app
from omnigent.server.omniroute_catalog import (
    _clear_cache_for_tests,
    fetch_omniroute_combo_catalog,
)
from omnigent.server.omniroute_routes import (
    CUSTOM_BEST_CODING_DISPLAY_NAME,
    RESERVED_NON_EXECUTABLE_ROUTE_IDS,
    executable_route_ids,
    is_executable_route_id,
)


def _minimal_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    """Build the ASGI app with a tmp web-ui dist so the static handler
    does not try to load the real SPA bundle."""
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(server_app, "_WEB_UI_DIST", tmp_path / "no-web-ui")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore("sqlite:///:memory:"),
        file_store=SqlAlchemyFileStore("sqlite:///:memory:"),
        conversation_store=SqlAlchemyConversationStore("sqlite:///:memory:"),
        artifact_store=artifact_store,
        host_store=HostStore("sqlite:///:memory:"),
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
    )


@pytest.fixture(autouse=True)
def _reset_catalog_cache() -> None:
    _clear_cache_for_tests()
    yield  # type: ignore[misc]
    _clear_cache_for_tests()


_LIVE_OMNIROUTE_PAYLOAD = {
    "object": "list",
    "data": [
        {
            "id": "auto/best-coding",
            "object": "model",
            "owned_by": "combo",
            "root": "auto/best-coding",
            "context_length": 1048576,
        },
        {
            "id": "auto/coding:fast",
            "object": "model",
            "owned_by": "combo",
            "root": "auto/coding:fast",
            "context_length": 1048576,
        },
        {
            "id": "auto/coding:reliable",
            "object": "model",
            "owned_by": "combo",
            "root": "auto/coding:reliable",
            "context_length": 1048576,
        },
        {
            "id": "custom/best-coding",
            "object": "model",
            "owned_by": "combo",
            "root": "custom/best-coding",
            "context_length": 200000,
        },
        {
            "id": "custom/outcome-scoring",
            "object": "model",
            "owned_by": "combo",
            "root": "custom/outcome-scoring",
            "context_length": 1048576,
        },
        # Concrete (non-combo) entries that the picker must exclude.
        {
            "id": "minimax/MiniMax-M3",
            "object": "model",
            "owned_by": "minimax",
            "root": "MiniMax-M3",
        },
        {
            "id": "codex/gpt-5.5",
            "object": "model",
            "owned_by": "codex",
            "root": "gpt-5.5",
        },
    ],
}


class _MockTransport(httpx.AsyncBaseTransport):
    """Pretend to be the OmniRoute ``GET /v1/models`` endpoint."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if request.url.path.endswith("/models"):
            body = json.dumps(self._payload).encode("utf-8")
            return httpx.Response(200, headers={"content-type": "application/json"}, content=body)
        return httpx.Response(404, content=b"")


@pytest.mark.asyncio
async def test_live_catalog_surfaces_custom_best_coding_for_picker() -> None:
    """Live fetch must include ``custom/best-coding`` (the canonical interactive route)."""
    transport = _MockTransport(_LIVE_OMNIROUTE_PAYLOAD)
    combos, source = await fetch_omniroute_combo_catalog(
        base_url="http://omniroute.test/v1",
        api_key="test-key",
        transport=transport,
    )
    assert source == "live"
    ids = {c.id for c in combos}
    assert "custom/best-coding" in ids, (
        "Live OmniRoute catalog must include the persisted custom/best-coding "
        "combo so the web UI picker can surface it for interactive execution."
    )
    # Concrete (non-combo) models must NOT bleed into the picker.
    assert "minimax/MiniMax-M3" not in ids
    assert "codex/gpt-5.5" not in ids


@pytest.mark.asyncio
async def test_live_catalog_excludes_outcome_scoring_from_executable() -> None:
    """``custom/outcome-scoring`` is background-only and MUST stay out of the executable set."""
    assert "custom/outcome-scoring" in RESERVED_NON_EXECUTABLE_ROUTE_IDS
    assert is_executable_route_id("custom/outcome-scoring") is False
    assert "custom/outcome-scoring" not in set(executable_route_ids())


def test_combo_payload_includes_canonical_custom_route() -> None:
    """``custom/best-coding`` is reachable through the static catalog (for
    routing/profile lookup) AND through the live OmniRoute catalog (for the
    picker). The curated fallback remains the three required ``auto/*``
    combos; ``custom/best-coding`` is supplemented by the live fetch."""
    from omnigent.server.omniroute_routes import OMNIROUTE_ROUTE_CATALOG

    profile = OMNIROUTE_ROUTE_CATALOG["custom/best-coding"]
    assert profile.display_name == CUSTOM_BEST_CODING_DISPLAY_NAME
    assert profile.route_kind == "custom_persisted"
    # The picker also surfaces this id when the live OmniRoute endpoint
    # is reachable (verified by ``test_live_catalog_surfaces_custom_best_coding_for_picker``).


# ── Session-create round-trip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_create_accepts_custom_best_coding_route(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical interactive route survives ``POST /v1/sessions`` validation.

    Regression for the "Value error, unknown native OmniRoute route id"
    failure that the live UI hit when the user selected "OmniRoute Coding
    Best" from the picker.
    """
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(server_app, "_WEB_UI_DIST", tmp_path / "no-web-ui")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        host_store=HostStore(db_uri),
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Use a fixture agent (opencode-native-ui is built-in) so the create
        # doesn't have to register a real agent.
        resp = await c.post(
            "/v1/sessions",
            json={
                "agent_id": "ag_opencode_test_placeholder",
                "title": "custom/best-coding regression",
                "harness_override": "opencode-native",
                "omniroute_route_id": "custom/best-coding",
                "reasoning_effort": "medium",
            },
        )
    # We do not assert 201 because no real agent row exists — but the schema
    # validator MUST accept the route (a 422 would be the regression we are
    # guarding against). Either a 201 (success) or a 404 (agent not found
    # but route accepted) proves the validator no longer rejects the route.
    assert resp.status_code in (201, 404), (
        f"session create returned unexpected status {resp.status_code}: {resp.text}"
    )
    assert "unknown native OmniRoute route id" not in resp.text


@pytest.mark.asyncio
async def test_session_create_rejects_custom_outcome_scoring_with_actionable_error(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Background-only evaluator route MUST fail closed with an actionable error."""
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(server_app, "_WEB_UI_DIST", tmp_path / "no-web-ui")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        host_store=HostStore(db_uri),
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/v1/sessions",
            json={
                "agent_id": "ag_opencode_test_placeholder",
                "harness_override": "opencode-native",
                "omniroute_route_id": "custom/outcome-scoring",
            },
        )
    assert resp.status_code == 422
    body = resp.text
    assert "unknown native OmniRoute route id" in body
    # The error MUST name the reserved background-only route so the failure
    # is actionable (the caller should switch to the M3 evaluator endpoint).
    assert "custom/outcome-scoring" in body
    # Display label must also be called out so an operator reading the
    # response recognizes the canonical wire-id vs display-name distinction.
    assert CUSTOM_BEST_CODING_DISPLAY_NAME in body


@pytest.mark.asyncio
async def test_session_create_canonicalizes_omniroute_transport_prefix(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``omniroute/custom/best-coding`` is normalized to ``custom/best-coding`` on accept."""
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(server_app, "_WEB_UI_DIST", tmp_path / "no-web-ui")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        host_store=HostStore(db_uri),
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/v1/sessions",
            json={
                "agent_id": "ag_opencode_test_placeholder",
                "harness_override": "opencode-native",
                "omniroute_route_id": "omniroute/custom/best-coding",
            },
        )
    # Must not 422 — the transport prefix is a compatibility alias.
    assert resp.status_code in (201, 404), (
        f"transport-prefix input returned {resp.status_code}: {resp.text}"
    )
    assert "unknown native OmniRoute route id" not in resp.text
