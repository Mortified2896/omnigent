"""Caller-scoped human outcome APIs with reload durability and validation."""

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.routes.sessions.routes_feedback import register_feedback_routes
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.fixture
def conversation_store(db_uri):
    return SqlAlchemyConversationStore(db_uri)


def seed_answer(store, conversation_id, response_id="answer"):
    store.append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="test",
                    content=[{"type": "output_text", "text": "Done"}],
                ),
            )
        ],
    )


def test_outcome_api_reload_and_validation(conversation_store):
    store = conversation_store
    conv = store.create_conversation()
    seed_answer(store, conv.id)
    app = FastAPI()
    router = APIRouter()
    register_feedback_routes(router, conversation_store=store)
    app.include_router(router, prefix="/v1")
    with TestClient(app) as client:
        url = f"/v1/sessions/{conv.id}"
        for outcome in ("not_sure", "success", "partial", "failed"):
            response = client.put(f"{url}/task-outcomes/answer", json={"outcome": outcome})
            assert response.status_code == 200
            assert client.get(f"{url}/task-experiment").json()[-1]["outcome"] == outcome
        assert (
            client.put(f"{url}/task-outcomes/answer", json={"outcome": "maybe"}).status_code == 422
        )
        nine_tags = {"outcome": "success", "tags": [str(i) for i in range(9)]}
        assert client.put(f"{url}/task-outcomes/answer", json=nine_tags).status_code == 422
        long_comment = {"outcome": "success", "comment": "x" * 4001}
        assert client.put(f"{url}/task-outcomes/answer", json=long_comment).status_code == 422
        assert (
            client.put(f"{url}/task-outcomes/missing", json={"outcome": "success"}).status_code
            == 404
        )
        assert client.get(f"{url}/response-feedback").json() == []


class Caller:
    def get_user_id(self, request):
        return request.headers.get("x-test-user")


def test_outcome_routes_are_caller_scoped(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    conv = store.create_conversation()
    other = store.create_conversation()
    seed_answer(store, conv.id)
    for user, level in [("alice", 2), ("bob", 2), ("reader", 1)]:
        permissions.grant(user, conv.id, level)
        permissions.grant(user, other.id, level)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def errors(request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    router = APIRouter()
    register_feedback_routes(
        router,
        conversation_store=store,
        auth_provider=Caller(),
        permission_store=permissions,
    )
    app.include_router(router, prefix="/v1")
    with TestClient(app) as client:
        url = f"/v1/sessions/{conv.id}"
        alice = {"x-test-user": "alice"}
        reader = {"x-test-user": "reader"}
        assert client.get(f"{url}/task-experiment").status_code == 401
        assert (
            client.get(f"{url}/task-experiment", headers={"x-test-user": "stranger"}).status_code
            == 404
        )
        success = {"outcome": "success"}
        assert (
            client.put(f"{url}/task-outcomes/answer", headers=alice, json=success).status_code
            == 200
        )
        assert (
            client.get(f"{url}/task-experiment", headers=alice).json()[0]["outcome"] == "success"
        )
        # Human revisions are visible only to their author, even at READ level.
        assert client.get(f"{url}/task-experiment", headers={"x-test-user": "bob"}).json() == []
        assert client.get(f"{url}/task-experiment", headers=reader).json() == []
        assert (
            client.put(
                f"{url}/task-outcomes/answer", headers=reader, json={"outcome": "failed"}
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/v1/sessions/{other.id}/task-outcomes/answer",
                headers=alice,
                json=success,
            ).status_code
            == 404
        )
