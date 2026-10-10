"""Prompt-tag acceptance edits metadata only, with authorization and a CAS guard."""

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


def test_prompt_tag_review_authorization_cas_and_reload(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    conv = store.create_conversation()
    other = store.create_conversation()
    prompt = store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="prompt",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Fix the mobile UI"}],
                    task_tags=["UI"],
                    task_tag_suggestions=["Testing"],
                ),
            )
        ],
    )[0]
    for user, level in [("editor", 2), ("reader", 1)]:
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
    url = f"/v1/sessions/{conv.id}/items/{prompt.id}/task-tags"
    body = {"expected_tags": ["UI"], "tags": ["UI", "Testing"]}
    with TestClient(app) as client:
        assert client.put(url, json=body).status_code == 401
        assert client.put(url, json=body, headers={"x-test-user": "reader"}).status_code == 403
        headers = {"x-test-user": "editor"}
        response = client.put(url, json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["task_tags"] == ["UI", "Testing"]
        assert "task_tag_suggestions" not in response.json()
        saved = store.get_item(conv.id, prompt.id)
        assert saved.data.content == prompt.data.content
        assert saved.created_at == prompt.created_at
        assert saved.data.task_tags == ["UI", "Testing"]
        assert client.put(url, json=body, headers=headers).status_code == 409
        assert (
            client.put(
                f"/v1/sessions/{other.id}/items/{prompt.id}/task-tags", json=body, headers=headers
            ).status_code
            == 404
        )
        assert (
            client.put(
                url, json={"expected_tags": [], "tags": ["x" * 41]}, headers=headers
            ).status_code
            == 422
        )
        assert store.get_item(conv.id, prompt.id).data.task_tags == ["UI", "Testing"]
