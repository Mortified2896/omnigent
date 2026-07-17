"""HTTP integration test for the ``GET /v1/omniroute/combos`` endpoint.

Drives the live catalog endpoint through the FastAPI ASGI transport —
same pattern as :mod:`tests.server.test_app`. Uses a mocked httpx
transport so the test never hits the real OmniRoute endpoint.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server import app as server_app
from omnigent.server.omniroute_catalog import (
    _clear_cache_for_tests,
)


def _build_minimal_app(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Minimal FastAPI app for the combos endpoint — no DB writes, just the route."""
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
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        host_store=HostStore(db_uri),
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
    )


@pytest.fixture(autouse=True)
def _reset_catalog_cache() -> None:
    _clear_cache_for_tests()
    yield  # type: ignore[misc]
    _clear_cache_for_tests()


@pytest.mark.asyncio
async def test_omniroute_combos_endpoint_returns_curated_when_endpoint_down(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when OmniRoute is unreachable, the endpoint returns the curated
    three combos (not an empty list, not a 500)."""

    def _boom(*args: object, **kwargs: object) -> httpx.AsyncClient:
        raise httpx.ConnectError("simulated offline", request=httpx.Request("GET", "http://x"))

    # Force every live fetch to fail by patching the catalog's transport builder.
    monkeypatch.setattr(server_app, "_WEB_UI_DIST", tmp_path / "no-web-ui")
    monkeypatch.setenv("OMNIGENT_OMNIROUTE_BASE_URL", "http://offline.test/v1")
    monkeypatch.setenv("OMNIGENT_OMNIROUTE_API_KEY", "test-key")

    from omnigent.server import omniroute_catalog

    async def _always_offline(
        base_url: str,
        api_key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        return [], "live"  # empty list = "treat as offline"

    monkeypatch.setattr(omniroute_catalog, "_fetch_live_catalog", _always_offline)

    app = _build_minimal_app(db_uri, tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/v1/omniroute/combos")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] in ("cache", "fallback_curated")
    ids = {entry["id"] for entry in body["combos"]}
    assert "auto/best-coding" in ids
    assert "auto/coding:fast" in ids
    assert "auto/coding:reliable" in ids
    # Each entry reports provider=omniroute + kind=combo so the picker
    # can route them through the curated branch.
    for entry in body["combos"]:
        assert entry["provider"] == "omniroute"
        assert entry["kind"] == "combo"


@pytest.mark.asyncio
async def test_omniroute_combos_endpoint_unauthenticated(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint is unauthenticated (matches ``/v1/info``) so the SPA can
    probe it before any session cookie is held."""
    from omnigent.server import omniroute_catalog

    monkeypatch.setattr(server_app, "_WEB_UI_DIST", tmp_path / "no-web-ui")
    monkeypatch.setenv("OMNIGENT_OMNIROUTE_BASE_URL", "http://offline.test/v1")

    async def _always_offline(
        base_url: str,
        api_key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        return [], "live"

    monkeypatch.setattr(omniroute_catalog, "_fetch_live_catalog", _always_offline)

    app = _build_minimal_app(db_uri, tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # No Authorization header — endpoint must still serve curated rows.
        resp = await c.get("/v1/omniroute/combos")
    assert resp.status_code == 200
    assert len(resp.json()["combos"]) >= 3
