"""Conditional session deletion preserves concurrent edits and descendants."""

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store import sqlalchemy_store as conversation_store_module
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.util.test_session_policy import test_session_labels as make_test_session_labels

AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"


def _app(db_uri: str) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    app.include_router(
        create_sessions_router(
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
        ),
        prefix="/v1",
    )
    return app


def _ensure_agent(db_uri: str) -> None:
    store = SqlAlchemyAgentStore(db_uri)
    if store.get(AGENT_ID) is None:
        store.create(agent_id=AGENT_ID, name="test-agent", bundle_location="test/bundle")


def test_conditional_delete_rejects_label_changes_and_then_deletes_current_snapshot(
    db_uri: str,
) -> None:
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(agent_id=AGENT_ID)
    store.set_labels(conv.id, make_test_session_labels("run", "codex"))

    with TestClient(_app(db_uri)) as client:
        snapshot = client.get(
            f"/v1/sessions/{conv.id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        assert snapshot.status_code == 200
        etag = snapshot.headers["etag"]

        store.set_labels(conv.id, {"omnigent.pinned.local": "true"})
        stale = client.delete(f"/v1/sessions/{conv.id}", headers={"If-Match": etag})
        assert stale.status_code == 412
        assert store.get_conversation(conv.id) is not None

        store.set_labels(conv.id, {"omnigent.pinned.local": ""})
        store.set_session_live_status(conv.id, "idle")
        current = client.get(
            f"/v1/sessions/{conv.id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        assert current.status_code == 200
        deleted = client.delete(
            f"/v1/sessions/{conv.id}",
            headers={"If-Match": current.headers["etag"]},
        )
        assert deleted.status_code == 200, deleted.text

    assert store.get_conversation(conv.id) is None


def test_conditional_delete_rejects_a_session_with_a_child(db_uri: str) -> None:
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(agent_id=AGENT_ID)
    store.set_labels(parent.id, make_test_session_labels("run", "codex"))
    store.create_conversation(
        kind="sub_agent",
        title="child",
        parent_conversation_id=parent.id,
        agent_id=AGENT_ID,
    )

    with TestClient(_app(db_uri)) as client:
        snapshot = client.get(
            f"/v1/sessions/{parent.id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        assert snapshot.status_code == 200
        response = client.delete(
            f"/v1/sessions/{parent.id}",
            headers={"If-Match": snapshot.headers["etag"]},
        )
        assert response.status_code == 412

    assert store.get_conversation(parent.id) is not None


def test_conditional_delete_rejects_a_same_second_conversation_edit(db_uri: str) -> None:
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(agent_id=AGENT_ID, title="before")
    store.set_labels(conv.id, make_test_session_labels("run", "codex"))

    with TestClient(_app(db_uri)) as client:
        snapshot = client.get(
            f"/v1/sessions/{conv.id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        assert snapshot.status_code == 200
        etag = snapshot.headers["etag"]

        # The store's monotonic version must reject this even when the title
        # edit and the snapshot happen in one epoch-second.
        assert store.update_conversation(conv.id, title="after") is not None
        stale = client.delete(f"/v1/sessions/{conv.id}", headers={"If-Match": etag})
        assert stale.status_code == 412

    assert store.get_conversation(conv.id) is not None


def test_conditional_delete_rejects_first_same_second_append(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's ETag includes first-turn content generation."""
    fixed_epoch = 1_760_000_000
    monkeypatch.setattr(conversation_store_module, "now_epoch", lambda: fixed_epoch)
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(agent_id=AGENT_ID)
    store.set_session_live_status(conv.id, "idle")

    with TestClient(_app(db_uri)) as client:
        snapshot = client.get(
            f"/v1/sessions/{conv.id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        assert snapshot.status_code == 200
        store.append(
            conv.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="first",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": "hello"}],
                    ),
                )
            ],
        )
        stale = client.delete(
            f"/v1/sessions/{conv.id}",
            headers={"If-Match": snapshot.headers["etag"]},
        )
        assert stale.status_code == 412

    assert store.get_conversation(conv.id) is not None


def test_conditional_delete_rejects_review_comment_mutation(db_uri: str) -> None:
    """The route's ETag includes review comment additions."""
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(agent_id=AGENT_ID)
    store.set_session_live_status(conv.id, "idle")
    comments = SqlAlchemyCommentStore(db_uri)

    with TestClient(_app(db_uri)) as client:
        snapshot = client.get(
            f"/v1/sessions/{conv.id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        assert snapshot.status_code == 200
        comments.add(conv.id, "README.md", "Keep this", 0, 4)
        stale = client.delete(
            f"/v1/sessions/{conv.id}",
            headers={"If-Match": snapshot.headers["etag"]},
        )
        assert stale.status_code == 412

    assert store.get_conversation(conv.id) is not None
