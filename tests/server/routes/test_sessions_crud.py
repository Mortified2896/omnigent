"""Tests for Sessions API CRUD endpoints (list, get, delete, patch).

Exercises the core session management routes through the ``client``
fixture. Since the lifespan event (which seeds agents) does not run
in test fixtures, we seed a test agent and conversation directly via
the stores.
"""

from __future__ import annotations

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest_asyncio.fixture()
async def session_id(db_uri: str) -> str:
    """Seed a test agent and conversation, return the session ID."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-agent", bundle_location="test:///bundle")
    conv = conv_store.create_conversation(agent_id=agent_id)
    return conv.id


# ── GET /v1/sessions (list) ─────────────────────────────────────────


async def test_list_sessions_empty(client: httpx.AsyncClient) -> None:
    """Empty database returns an empty list."""
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


async def test_list_sessions_after_create(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """A created session appears in the list."""
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    ids = [s["id"] for s in body["data"]]
    assert session_id in ids


async def test_list_sessions_pagination(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Pagination with limit returns at most N sessions."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="pag-agent", bundle_location="test:///bundle")
    conv_store.create_conversation(agent_id=agent_id)
    conv_store.create_conversation(agent_id=agent_id)

    resp = await client.get("/v1/sessions?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1


# ── GET /v1/sessions/{id} (get snapshot) ────────────────────────────


async def test_get_session(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Get a session by ID returns its snapshot."""
    resp = await client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == session_id


async def test_get_session_not_found(client: httpx.AsyncClient) -> None:
    """Getting a nonexistent session returns 404."""
    resp = await client.get("/v1/sessions/conv_nonexistent_12345")
    assert resp.status_code == 404


# ── DELETE /v1/sessions/{id} ────────────────────────────────────────


async def test_delete_session(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Deleting a session returns 200 with deleted: true."""
    resp = await client.delete(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True


async def test_delete_session_not_found(client: httpx.AsyncClient) -> None:
    """Deleting a nonexistent session returns 404."""
    resp = await client.delete("/v1/sessions/conv_nonexistent_12345")
    assert resp.status_code == 404


# ── POST /v1/sessions/bulk-delete ────────────────────────────────────


async def test_bulk_delete_multiple_sessions(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Bulk-deleting multiple valid sessions deletes them all."""
    from omnigent.db.utils import generate_agent_id
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-agent", bundle_location="test:///bundle")
    a = conv_store.create_conversation(agent_id=agent_id)
    b = conv_store.create_conversation(agent_id=agent_id)
    c = conv_store.create_conversation(agent_id=agent_id)

    resp = await client.post(
        "/v1/sessions/bulk-delete",
        json={"ids": [a.id, b.id, c.id]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["deleted"]) == sorted([a.id, b.id, c.id])
    assert body["failed"] == []

    # All three are gone from the store.
    assert conv_store.get_conversation(a.id) is None
    assert conv_store.get_conversation(b.id) is None
    assert conv_store.get_conversation(c.id) is None


async def test_bulk_delete_empty_input(client: httpx.AsyncClient) -> None:
    """Bulk-deleting with an empty list returns empty results."""
    resp = await client.post(
        "/v1/sessions/bulk-delete",
        json={"ids": []},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == []
    assert body["failed"] == []


async def test_bulk_delete_with_nonexistent_ids(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Bulk-deleting reports nonexistent IDs as failed."""
    resp = await client.post(
        "/v1/sessions/bulk-delete",
        json={"ids": ["conv_nonexistent_1", "conv_nonexistent_2"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == []
    assert len(body["failed"]) == 2
    for f in body["failed"]:
        assert f["id"].startswith("conv_nonexistent_")
        assert f["error"]


async def test_bulk_delete_mixed_valid_and_invalid(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Bulk-deleting a mix of valid and nonexistent IDs partial-succeeds."""
    from omnigent.db.utils import generate_agent_id
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-agent", bundle_location="test:///bundle")
    valid = conv_store.create_conversation(agent_id=agent_id)

    resp = await client.post(
        "/v1/sessions/bulk-delete",
        json={"ids": [valid.id, "conv_ghost"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == [valid.id]
    assert len(body["failed"]) == 1
    assert body["failed"][0]["id"] == "conv_ghost"
    assert conv_store.get_conversation(valid.id) is None


async def test_bulk_delete_preserves_unrelated_sessions(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Bulk-deleting leaves non-target sessions untouched."""
    from omnigent.db.utils import generate_agent_id
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-agent", bundle_location="test:///bundle")
    to_delete = conv_store.create_conversation(agent_id=agent_id)
    to_keep = conv_store.create_conversation(agent_id=agent_id)

    resp = await client.post(
        "/v1/sessions/bulk-delete",
        json={"ids": [to_delete.id]},
    )
    assert resp.status_code == 200
    assert to_delete.id in resp.json()["deleted"]

    # The non-target session still exists.
    assert conv_store.get_conversation(to_keep.id) is not None


# ── PATCH /v1/sessions/{id} ─────────────────────────────────────────


async def test_patch_session_title(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Patching a session's title returns the updated session."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "New Title"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200


async def test_patch_session_not_found(client: httpx.AsyncClient) -> None:
    """Patching a nonexistent session returns 404."""
    resp = await client.patch(
        "/v1/sessions/conv_nonexistent_12345",
        json={"title": "New Title"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 404


# ── GET /v1/sessions/projects ────────────────────────────────────────


async def test_list_projects_empty(client: httpx.AsyncClient) -> None:
    """No project labels anywhere → empty project list."""
    resp = await client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_projects_returns_names_sorted(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Projects surface as a sorted list of names."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    a = conv_store.create_conversation()
    b = conv_store.create_conversation()
    conv_store.set_labels(a.id, {"omni_project": "Sprint 42"})
    conv_store.set_labels(b.id, {"omni_project": "Customer X"})

    resp = await client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == ["Customer X", "Sprint 42"]


# ── GET /v1/sessions?project= (filter) ───────────────────────────────


async def test_list_sessions_filtered_by_project(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``?project=X`` returns only sessions in that project."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    # GET /v1/sessions filters has_agent_id=True, so bind the conversations to
    # a seeded agent — otherwise the list comes back empty.
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="project-agent", bundle_location="test:///bundle")
    filed = conv_store.create_conversation(agent_id=agent_id)
    conv_store.create_conversation(agent_id=agent_id)  # unfiled
    conv_store.set_labels(filed.id, {"omni_project": "X"})

    resp = await client.get("/v1/sessions?project=X")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["data"]]
    assert ids == [filed.id]


async def test_list_sessions_empty_project_returns_unfiled(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``?project=`` (empty) returns only sessions with no project label."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="project-agent", bundle_location="test:///bundle")
    filed = conv_store.create_conversation(agent_id=agent_id)
    unfiled = conv_store.create_conversation(agent_id=agent_id)
    conv_store.set_labels(filed.id, {"omni_project": "X"})

    resp = await client.get("/v1/sessions?project=")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["data"]]
    assert unfiled.id in ids
    assert filed.id not in ids


# ── PATCH /v1/sessions/{id} project label ────────────────────────────


async def test_patch_session_sets_project_label(
    client: httpx.AsyncClient,
    session_id: str,
    db_uri: str,
) -> None:
    """PATCH with ``labels: {project: X}`` upserts the project label."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {"omni_project": "Sprint 42"}},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(session_id)
    assert conv is not None
    assert conv.labels.get("omni_project") == "Sprint 42"


async def test_patch_session_empty_project_removes_label(
    client: httpx.AsyncClient,
    session_id: str,
    db_uri: str,
) -> None:
    """PATCH with ``labels: {project: ""}`` removes the project label rather
    than persisting an empty value — so the session returns to Unfiled."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_labels(session_id, {"omni_project": "Sprint 42"})

    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {"omni_project": ""}},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    conv = conv_store.get_conversation(session_id)
    assert conv is not None
    assert "omni_project" not in conv.labels
