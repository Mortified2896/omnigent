"""Real-store API coverage for orthogonal exclusion and caller isolation."""

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.routes.sessions.routes_feedback import register_feedback_routes
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.util.test_session_policy import test_session_labels as make_test_labels


def seed_answer(store, conversation_id):
    store.append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id="answer",
                data=MessageData(
                    role="assistant",
                    agent="test",
                    content=[{"type": "output_text", "text": "Done"}],
                ),
            )
        ],
    )


def client_for(store, auth=None, permissions=None):
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def errors(request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    router = APIRouter()
    register_feedback_routes(
        router, conversation_store=store, auth_provider=auth, permission_store=permissions
    )
    app.include_router(router, prefix="/v1")
    return TestClient(app)


def test_exclusion_before_outcome_reload_and_raw_review_preservation(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    seed_answer(store, conv.id)
    url = f"/v1/sessions/{conv.id}"
    with client_for(store) as client:
        assert client.get(f"{url}/scoring-policy").json()["responses"] == {}
        response = client.put(
            f"{url}/scoring-eligibility/answer",
            json={
                "score_eligible": False,
                "exclusion_reason": "test_fixture",
            },
        )
        assert response.status_code == 200
        assert "outcome" not in response.json()
        assert client.get(f"{url}/scored-outcomes").json() == []
        body = {"outcome": "success", "comment": "Keep this note", "tags": ["Verification"]}
        assert client.put(f"{url}/task-outcomes/answer", json=body).status_code == 200
        assert client.get(f"{url}/scored-outcomes").json() == []
    reopened = SqlAlchemyConversationStore(store.storage_location)
    with client_for(reopened) as client:
        policy = client.get(f"{url}/scoring-policy").json()
        assert policy["responses"]["answer"] == {
            "score_eligible": False,
            "exclusion_reason": "test_fixture",
        }
        assert (
            client.put(
                f"{url}/scoring-eligibility/answer", json={"score_eligible": True}
            ).status_code
            == 200
        )
        assert client.get(f"{url}/scored-outcomes").json()[0]["first_attempt_success"] == 1
        rows = client.get(f"{url}/task-experiment").json()
        outcomes = [r for r in rows if r["kind"] == "outcome"]
        assert len(outcomes) == 1
        assert all(outcomes[0][key] == value for key, value in body.items())
        # Exclusion remains orthogonal after a later outcome edit.
        assert (
            client.put(
                f"{url}/scoring-eligibility/answer", json={"score_eligible": False}
            ).status_code
            == 200
        )
        assert (
            client.put(f"{url}/task-outcomes/answer", json={"outcome": "failed"}).status_code
            == 200
        )
        assert client.get(f"{url}/scored-outcomes").json() == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"score_eligible": "false"},
        {"score_eligible": 0},
        {"score_eligible": False, "exclusion_reason": "guess"},
        {"score_eligible": True, "exclusion_reason": "other"},
        {"score_eligible": False, "tags": ["not allowed here"]},
    ],
)
def test_validation_does_not_append_invalid_revisions(db_uri, body):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    seed_answer(store, conv.id)
    url = f"/v1/sessions/{conv.id}"
    with client_for(store) as client:
        assert client.put(f"{url}/scoring-eligibility/answer", json=body).status_code == 422
        assert client.get(f"{url}/task-experiment").json() == []


def test_test_session_exclusion_applies_even_to_legacy_outcomes(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    seed_answer(store, conv.id)
    url = f"/v1/sessions/{conv.id}"
    with client_for(store) as client:
        assert (
            client.put(f"{url}/task-outcomes/answer", json={"outcome": "success"}).status_code
            == 200
        )
        assert len(client.get(f"{url}/scored-outcomes").json()) == 1
        store.set_labels(conv.id, make_test_labels("run", "codex"))
        assert client.get(f"{url}/scored-outcomes").json() == []
        assert client.get(f"{url}/scoring-policy").json()["is_test"] is True
        assert (
            client.put(
                f"{url}/scoring-eligibility/answer", json={"score_eligible": True}
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"{url}/scoring-eligibility/missing", json={"score_eligible": False}
            ).status_code
            == 404
        )


class Caller:
    def get_user_id(self, request):
        return request.headers.get("x-test-user")


def test_scoring_routes_are_caller_scoped_and_permission_gated(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    conv, other = store.create_conversation(), store.create_conversation()
    seed_answer(store, conv.id)
    for user, level in [("alice", 2), ("bob", 2), ("reader", 1)]:
        permissions.grant(user, conv.id, level)
        permissions.grant(user, other.id, level)
    url = f"/v1/sessions/{conv.id}"
    with client_for(store, Caller(), permissions) as client:
        for suffix in ("scoring-policy", "scored-outcomes"):
            assert client.get(f"{url}/{suffix}").status_code == 401
            assert (
                client.get(f"{url}/{suffix}", headers={"x-test-user": "stranger"}).status_code
                == 404
            )
        payload = {"score_eligible": False}
        assert (
            client.put(
                f"{url}/scoring-eligibility/answer",
                json=payload,
                headers={"x-test-user": "reader"},
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"{url}/scoring-eligibility/answer", json=payload, headers={"x-test-user": "alice"}
            ).status_code
            == 200
        )
        assert (
            client.get(f"{url}/scoring-policy", headers={"x-test-user": "bob"}).json()["responses"]
            == {}
        )
        assert (
            client.get(f"{url}/scoring-policy", headers={"x-test-user": "alice"}).json()[
                "responses"
            ]["answer"]["score_eligible"]
            is False
        )
        assert (
            client.put(
                f"/v1/sessions/{other.id}/scoring-eligibility/answer",
                json=payload,
                headers={"x-test-user": "alice"},
            ).status_code
            == 404
        )
