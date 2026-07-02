"""Tests for the per-server session-sharing mode gate.

Covers the whole feature surface:

- :meth:`SharingMode.coerce` — the fail-open-to-ON contract for the
  env-var and callable boundaries.
- ``create_app(sharing_mode=…)`` wiring — static value, per-request
  callable, and the ``OMNIGENT_SHARING_MODE`` env-var default.
- ``GET /v1/info`` reporting ``sharing_mode`` so the web app stays in
  lockstep with the server gate.
- The ``PUT /v1/sessions/{id}/permissions`` gate: ``OFF`` rejects all
  new grants (403), ``READ_ONLY`` caps grants at read (edit → 403,
  read → ok), and ``ON`` is behavior-preserving. Revoke stays allowed
  in every mode.

The app is built via the real :func:`create_app` so the tests exercise
the actual ``app.state.sharing_mode`` normalization and the route gate,
not a hand-rolled stub. Requests go through ``httpx.ASGITransport`` (no
lifespan) since none of these paths need the runtime.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    AuthProvider,
    SharingMode,
    UnifiedAuthProvider,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

# Reserved test identities. The owner is granted MANAGE so it can reach
# the grant endpoint; the grantee is the target of each new grant.
_OWNER = "owner@sharing.test"
_GRANTEE = "bob@sharing.test"


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    sharing_mode: SharingMode | object | None = None,
    permission_store: SqlAlchemyPermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> FastAPI:
    """Build a real ``create_app`` wired to per-test SQLite stores."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=permission_store,
        auth_provider=auth_provider,
        sharing_mode=sharing_mode,
    )


def _client(app: FastAPI, email: str | None = None) -> httpx.AsyncClient:
    """An in-process async client, optionally carrying a header identity."""
    headers = {"X-Forwarded-Email": email} if email else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    )


def _seed_owned_session(
    db_uri: str,
    tmp_path: Path,
    *,
    sharing_mode: SharingMode,
) -> tuple[FastAPI, str]:
    """Build an app whose ``_OWNER`` identity manages a real session.

    Seeds a conversation and an OWNER grant directly into the shared DB
    so ``PUT …/permissions`` gets past the manage-access check and hits
    the sharing gate (and, when allowed, actually persists the grant).
    """
    permission_store = SqlAlchemyPermissionStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    conv = conversation_store.create_conversation()
    permission_store.ensure_user(_OWNER)
    permission_store.grant(_OWNER, conv.id, LEVEL_OWNER)
    app = _build_app(
        db_uri,
        tmp_path,
        sharing_mode=sharing_mode,
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    return app, conv.id


# ── SharingMode.coerce — fail-open-to-ON contract ────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (SharingMode.OFF, SharingMode.OFF),
        (SharingMode.READ_ONLY, SharingMode.READ_ONLY),
        (SharingMode.ON, SharingMode.ON),
        ("off", SharingMode.OFF),
        ("read_only", SharingMode.READ_ONLY),
        ("on", SharingMode.ON),
        ("READ_ONLY", SharingMode.READ_ONLY),  # case-insensitive
        (" On ", SharingMode.ON),  # whitespace-tolerant
        (None, SharingMode.ON),  # unset → fail open
        ("", SharingMode.ON),  # empty → fail open
        ("garbage", SharingMode.ON),  # unrecognized → fail open
        (123, SharingMode.ON),  # wrong type → fail open
    ],
)
def test_coerce_fails_open_to_on(value: object, expected: SharingMode) -> None:
    """Anything unset/unrecognized coerces to ON; valid values round-trip."""
    assert SharingMode.coerce(value) is expected


# ── create_app wiring: env default / static / callable ───────────────


def test_wiring_defaults_to_on_when_env_unset(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No arg + unset env → the top-level default is ON."""
    monkeypatch.delenv("OMNIGENT_SHARING_MODE", raising=False)
    app = _build_app(db_uri, tmp_path)
    assert app.state.sharing_mode() is SharingMode.ON


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("off", SharingMode.OFF),
        ("read_only", SharingMode.READ_ONLY),
        ("on", SharingMode.ON),
        ("nonsense", SharingMode.ON),  # fail open
    ],
)
def test_wiring_reads_env_var(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: SharingMode,
) -> None:
    """``OMNIGENT_SHARING_MODE`` is the top-level control when no arg is given."""
    monkeypatch.setenv("OMNIGENT_SHARING_MODE", raw)
    app = _build_app(db_uri, tmp_path)
    assert app.state.sharing_mode() is expected


def test_wiring_static_value_overrides_env(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``sharing_mode=`` beats the env var."""
    monkeypatch.setenv("OMNIGENT_SHARING_MODE", "off")
    app = _build_app(db_uri, tmp_path, sharing_mode=SharingMode.READ_ONLY)
    assert app.state.sharing_mode() is SharingMode.READ_ONLY


def test_wiring_callable_is_resolved_per_request(db_uri: str, tmp_path: Path) -> None:
    """A callable is invoked (and coerced) on each resolution, not cached."""
    modes = iter(["on", "off", "garbage"])
    app = _build_app(db_uri, tmp_path, sharing_mode=lambda: next(modes))
    assert app.state.sharing_mode() is SharingMode.ON
    assert app.state.sharing_mode() is SharingMode.OFF
    # The callable boundary also fails open for a bad value.
    assert app.state.sharing_mode() is SharingMode.ON


# ── GET /v1/info reports the mode ────────────────────────────────────


async def test_info_reports_default_on(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_SHARING_MODE", raising=False)
    app = _build_app(db_uri, tmp_path)
    async with _client(app) as c:
        resp = await c.get("/v1/info")
        assert resp.status_code == 200
        assert resp.json()["sharing_mode"] == "on"


@pytest.mark.parametrize(
    "mode,expected",
    [
        (SharingMode.OFF, "off"),
        (SharingMode.READ_ONLY, "read_only"),
        (SharingMode.ON, "on"),
    ],
)
async def test_info_reports_configured_mode(
    db_uri: str, tmp_path: Path, mode: SharingMode, expected: str
) -> None:
    app = _build_app(db_uri, tmp_path, sharing_mode=mode)
    async with _client(app) as c:
        resp = await c.get("/v1/info")
        assert resp.json()["sharing_mode"] == expected


# ── The grant gate — no permission store needed (gate precedes it) ───


async def test_off_rejects_new_grant_at_any_level(db_uri: str, tmp_path: Path) -> None:
    """OFF blocks a new grant regardless of level, before the store check."""
    app = _build_app(db_uri, tmp_path, sharing_mode=SharingMode.OFF)
    async with _client(app) as c:
        for level in (LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE):
            resp = await c.put(
                "/v1/sessions/conv_absent/permissions",
                json={"user_id": _GRANTEE, "level": level},
            )
            assert resp.status_code == 403, resp.text
            assert "disabled" in resp.text.lower()


async def test_read_only_rejects_edit_grant(db_uri: str, tmp_path: Path) -> None:
    """READ_ONLY rejects an edit (level > read) grant with 403."""
    app = _build_app(db_uri, tmp_path, sharing_mode=SharingMode.READ_ONLY)
    async with _client(app) as c:
        resp = await c.put(
            "/v1/sessions/conv_absent/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_EDIT},
        )
        assert resp.status_code == 403, resp.text
        assert "read-only" in resp.text.lower()


# ── The grant gate — allowed paths persist against a real store ──────


async def test_on_allows_edit_grant(db_uri: str, tmp_path: Path) -> None:
    """ON is behavior-preserving: an edit grant succeeds (200)."""
    app, sid = _seed_owned_session(db_uri, tmp_path, sharing_mode=SharingMode.ON)
    async with _client(app, _OWNER) as c:
        resp = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_EDIT},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["level"] == LEVEL_EDIT


async def test_read_only_allows_read_but_not_edit(db_uri: str, tmp_path: Path) -> None:
    """READ_ONLY lets a read grant through but still rejects edit."""
    app, sid = _seed_owned_session(db_uri, tmp_path, sharing_mode=SharingMode.READ_ONLY)
    async with _client(app, _OWNER) as c:
        ok = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["level"] == LEVEL_READ

        denied = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_EDIT},
        )
        assert denied.status_code == 403, denied.text


async def test_off_rejects_grant_even_with_manage_access(db_uri: str, tmp_path: Path) -> None:
    """Even a legitimate manager cannot create a grant when sharing is OFF."""
    app, sid = _seed_owned_session(db_uri, tmp_path, sharing_mode=SharingMode.OFF)
    async with _client(app, _OWNER) as c:
        resp = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        assert resp.status_code == 403, resp.text


async def test_revoke_is_unaffected_by_read_only(db_uri: str, tmp_path: Path) -> None:
    """Revoke stays allowed in READ_ONLY — only *new* grants are gated."""
    app, sid = _seed_owned_session(db_uri, tmp_path, sharing_mode=SharingMode.READ_ONLY)
    async with _client(app, _OWNER) as c:
        await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        revoke = await c.delete(f"/v1/sessions/{sid}/permissions/{_GRANTEE}")
        assert revoke.status_code == 204, revoke.text
