"""Forecast ordering, exact native identity and caller-scoped outcome APIs."""

import asyncio

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.routes.sessions.routes_feedback import register_feedback_routes
from omnigent.server.schemas import SessionEventInput
from omnigent.server.task_experiment import list_experiment_events
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture
def conversation_store(db_uri):
    return SqlAlchemyConversationStore(db_uri)


def test_forecast_commits_before_native_inference(conversation_store, monkeypatch):
    from omnigent.server.routes._sessions import orchestration as flow

    store = conversation_store
    conv = store.create_conversation()
    conv.harness_override = "codex-native"
    conv.model_override = "codex/test-model"
    conv.reasoning_effort = "low"
    body = SessionEventInput(
        type="message",
        success_forecast={"probability": 78},
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "Test"}],
            "stable_id": "b" * 32,
        },
    )

    async def inference(*args, **kwargs):
        rows = list_experiment_events(store, conv.id)
        assert len(rows) == 1
        assert rows[0]["human_probability"] == 78
        assert rows[0]["selected_reasoning_effort"] == "low"
        event = flow._build_native_terminal_message_event(conv, body)
        assert "success_forecast" not in str(event)
        assert "probability" not in str(event)
        assert event["experiment_attempt_id"] == rows[0]["attempt_id"]
        from omnigent.server.task_experiment import accept_response_link

        accept_response_link(
            store,
            conv.id,
            {
                "attempt_id": event["experiment_attempt_id"],
                "native_response_id": "codex_exact-rpc-turn",
            },
        )

    monkeypatch.setattr(flow, "_forward_native_terminal_message", inference)

    async def run():
        async with httpx.AsyncClient() as client:
            await flow._dispatch_session_event_to_runner_impl(
                conv.id,
                conv,
                body,
                store,
                client,
                agent_name="test",
                file_store=None,
                artifact_store=None,
                created_by="alice",
                native_terminal_ready=True,
            )

    asyncio.run(run())
    rows = list_experiment_events(store, conv.id)
    assert len(rows) == 2
    assert rows[1]["native_response_id"] == "codex_exact-rpc-turn"
    assert rows[1]["attempt_id"] == rows[0]["attempt_id"]


def test_outcome_api_reload_and_validation(conversation_store):
    store = conversation_store
    conv = store.create_conversation()
    store.append(
        conv.id,
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
        assert (
            client.put(f"{url}/task-outcomes/missing", json={"outcome": "success"}).status_code
            == 404
        )
        assert client.get(f"{url}/response-feedback").json() == []
