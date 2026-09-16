"""Sessions API feedback authorization and persistence contract."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class Caller:
    def get_user_id(self, request):
        return request.headers.get("x-test-user")


def test_api_permissions_and_round_trip(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    conv = store.create_conversation()
    other = store.create_conversation()
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="answer",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "output_text", "text": "Done"}],
                ),
            )
        ],
    )
    for user, level in [("alice", 2), ("bob", 2), ("reader", 1)]:
        permissions.grant(user, conv.id, level)
        permissions.grant(user, other.id, level)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def errors(request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    app.include_router(
        create_sessions_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            auth_provider=Caller(),
            permission_store=permissions,
        ),
        prefix="/v1",
    )
    with TestClient(app) as client:
        url = f"/v1/sessions/{conv.id}/response-feedback"
        alice = {"x-test-user": "alice"}
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"x-test-user": "stranger"}).status_code == 404
        assert client.get(url, headers={"x-test-user": "reader"}).status_code == 200
        assert (
            client.put(
                url + "/answer", headers={"x-test-user": "reader"}, json={"rating": 1}
            ).status_code
            == 403
        )
        assert client.delete(url + "/answer", headers={"x-test-user": "reader"}).status_code == 403
        for rating in [0, 2, True, "1"]:
            assert (
                client.put(url + "/answer", headers=alice, json={"rating": rating}).status_code
                == 422
            )
        first = client.put(url + "/answer", headers=alice, json={"rating": 1}).json()
        assert client.put(url + "/answer", headers=alice, json={"rating": 1}).json() == first
        assert (
            client.put(
                url + "/answer", headers=alice, json={"rating": -1, "comment": "Too vague"}
            ).status_code
            == 200
        )
        assert client.get(url, headers=alice).json()[0]["comment"] == "Too vague"
        assert client.get(url, headers={"x-test-user": "bob"}).json() == []
        assert (
            client.put(
                f"/v1/sessions/{other.id}/response-feedback/answer",
                headers=alice,
                json={"rating": 1},
            ).status_code
            == 404
        )
        assert client.delete(url + "/answer", headers=alice).status_code == 204
        assert client.get(url, headers=alice).json() == []
