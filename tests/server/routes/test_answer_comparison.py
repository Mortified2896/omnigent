"""Jev disagreement pairs use the frozen choices once and keep normal chats intact."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.answer_comparison import (
    answer_request,
    comparison_prompt,
    parse_answer,
    text_task_eligible,
)
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.model_advisor_repository import AdvisorRepository
from omnigent.server.answer_comparison import LABEL, append_event, read_comparison
from omnigent.server.model_advisor_service import ModelAdvisorService
from omnigent.server.response_attribution import response_attribution_item
from omnigent.server.routes.model_advisor import create_model_advisor_router
from omnigent.server.routes.sessions.routes_feedback import register_feedback_routes
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.routes.test_jev_model_advisor import preferences
from tests.server.routes.test_model_advisor import (
    ALICE,
    JSON_ALICE,
    PROVIDER_CATALOG_MODELS,
    PROVIDER_GLM,
    PROVIDER_OPENAI,
    _FakeConnection,
    _FakeHost,
    _FakeHostStore,
    _FakeRegistry,
    _HeaderAuth,
)

HOST_ID = "1234567890abcdef1234567890abcdef"


def settle(client, round_id):
    for _ in range(100):
        payload = client.get(
            f"/v1/model-advisor/rounds/{round_id}?host_id={HOST_ID}", headers=ALICE
        ).json()
        if payload["state"] != "advisor_pending" and "advisor_overhead" in payload:
            return payload
        time.sleep(0.01)
    raise AssertionError("round did not settle")


@pytest.fixture
def setup_comparison(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    repository = AdvisorRepository(store._engine)
    conn = _FakeConnection(
        {
            "status": "ok",
            "raw_output": json.dumps(
                {"candidate_id": PROVIDER_GLM.choice_id, "rationale": "Jev receipt."}
            ),
        }
    )
    conn.host_id = HOST_ID
    registry = _FakeRegistry(conn, models=PROVIDER_CATALOG_MODELS)
    launches = []

    async def launcher(body, *, user_id, request=None):
        launches.append(body)
        conv = store.create_conversation(labels=body.labels, host_id=HOST_ID, workspace="/fixture")
        permissions.grant("alice@test", conv.id, 4)
        permissions.grant("reader@test", conv.id, 1)
        store.append(
            conv.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="",
                    created_by=user_id,
                    data=MessageData(role="user", content=body.initial_items[0].data["content"]),
                )
            ],
        )
        return SimpleNamespace(id=conv.id)

    service = ModelAdvisorService(
        repository=repository,
        host_store=_FakeHostStore([_FakeHost(HOST_ID, "alice@test")]),
        host_registry=registry,
        conversation_store=store,
        session_launcher=launcher,
    )
    app = FastAPI()
    app.state.model_advisor_service = service

    @app.exception_handler(OmnigentError)
    async def errors(request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    app.include_router(
        create_model_advisor_router(auth_provider=_HeaderAuth(), service=service), prefix="/v1"
    )
    router = APIRouter()
    register_feedback_routes(
        router, conversation_store=store, auth_provider=_HeaderAuth(), permission_store=permissions
    )
    app.include_router(router, prefix="/v1")
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, store=store, registry=registry, conn=conn, launches=launches
        )


def create_answer(
    setup, task="Explain why diversification reduces investment risk.", *, human_percent=0
):
    client = setup.client
    pref = preferences()
    pref["human_probability_percent"] = human_percent
    for provider in pref["providers"].values():
        provider["transport_preference"] = "direct_only"
    created = client.post(
        "/v1/model-advisor/rounds",
        headers=JSON_ALICE,
        json={
            "host_id": HOST_ID,
            "task": task,
            "human_choice_id": PROVIDER_OPENAI.choice_id,
            "preferences": pref,
        },
    )
    assert created.status_code == 200, created.text
    round_id = created.json()["round_id"]
    settled = settle(client, round_id)
    confirmed = client.post(
        f"/v1/model-advisor/rounds/{round_id}/confirm",
        headers=JSON_ALICE,
        json={
            "host_id": HOST_ID,
            "expected_version": settled["version"],
            "launch": {"agent_id": "ag_1", "workspace": "/fixture"},
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    session = confirmed.json()["execution"]["session_id"]
    assert session is not None, confirmed.json()
    executed_model = PROVIDER_OPENAI.model_id if human_percent == 100 else PROVIDER_GLM.model_id
    setup.store.append(
        session,
        [
            NewConversationItem(
                type="message",
                response_id="answer",
                data=MessageData(
                    role="assistant",
                    agent="fixture",
                    content=[{"type": "output_text", "text": "Original Jev answer"}],
                ),
            ),
            response_attribution_item(
                conversation_id=session,
                response_id="answer",
                requested_model=executed_model,
                actual_model=executed_model,
                model_source="response_usage",
                advisor_round_id=round_id,
            ),
        ],
    )
    return session, f"/v1/sessions/{session}/answer-comparisons/answer"


def wait_comparison(client, url):
    for _ in range(100):
        value = client.get(url, headers=ALICE).json()
        if value and value["state"] not in {"eligible", "pending"}:
            return value
        time.sleep(0.01)
    raise AssertionError("Comparison did not finish")


def test_exact_counterfactual_once_votes_reload_and_no_executor_changes(setup_comparison):
    setup = setup_comparison
    session, url = create_answer(setup)
    assert setup.client.get(url, headers=ALICE).json()["state"] == "eligible"
    setup.conn.reply = {
        "status": "ok",
        "raw_output": json.dumps({"eligible": True, "answer": "Human model answer"}),
    }
    for _ in range(2):
        assert setup.client.post(url, headers=JSON_ALICE, json={}).status_code == 200
    pair = wait_comparison(setup.client, url)
    assert pair["state"] == "ready"
    assert {row["answer"] for row in pair["candidates"]} == {
        "Original Jev answer",
        "Human model answer",
    }
    assert all("model" not in row and "arm" not in row for row in pair["candidates"])
    assert setup.registry.advisor_calls == 2  # One Jev decision, one other answer.
    assert len(setup.launches) == 1
    assert setup.registry.advisor_requests[-1] == answer_request(
        "Explain why diversification reduces investment risk."
    )
    human_slot = next(
        row["slot"] for row in pair["candidates"] if row["answer"] == "Human model answer"
    )
    saved = setup.client.put(
        url + "/preference",
        headers=JSON_ALICE,
        json={"choice": human_slot, "comment": "Clearer explanation"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["vote"]["preferred_arm"] == "human"
    assert setup.client.get(url, headers=ALICE).json()["vote"]["comment"] == "Clearer explanation"
    assert {row["model"] for row in saved.json()["candidates"]} == {
        PROVIDER_GLM.model_id,
        PROVIDER_OPENAI.model_id,
    }
    for choice in ("tie", "neither", "skip"):
        revised = setup.client.put(
            url + "/preference", headers=JSON_ALICE, json={"choice": choice}
        )
        assert revised.json()["vote"]["preferred_arm"] is None
    raw = read_comparison(setup.store, session, "answer", "alice@test")
    assert raw["vote"]["choice"] == "skip"
    assert raw["presentation"] == "original_answer_then_pair"
    assert setup.client.get(f"/v1/sessions/{session}/scored-outcomes", headers=ALICE).json() == []
    assert setup.registry.advisor_calls == 2 and len(setup.launches) == 1


@pytest.mark.parametrize(
    "task",
    ["Implement a new search page.", "Send an email to my professor.", "What is today's weather?"],
)
def test_actions_and_coding_never_compare(setup_comparison, task):
    setup = setup_comparison
    session, url = create_answer(setup, task)
    assert LABEL not in setup.store.get_conversation(session).labels
    assert setup.client.get(url, headers=ALICE).json() is None
    assert setup.client.post(url, headers=JSON_ALICE, json={}).json() is None
    assert setup.registry.advisor_calls == 1


def test_noninitial_response_and_tool_turn_are_excluded(setup_comparison):
    setup = setup_comparison
    session, url = create_answer(setup)
    assert setup.client.get(url.rsplit("/", 1)[0] + "/later", headers=ALICE).json() is None
    setup.store.append(
        session,
        [
            NewConversationItem(
                type="message",
                response_id="",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Explain more"}],
                ),
            )
        ],
    )
    assert setup.client.get(url, headers=ALICE).json() is None
    assert setup.client.post(url, headers=JSON_ALICE, json={}).json() is None
    assert setup.registry.advisor_calls == 1


def test_failure_and_permissions_preserve_original_answer(setup_comparison):
    setup = setup_comparison
    session, url = create_answer(setup)
    assert setup.client.get(url).status_code == 401
    reader = {"x-test-user": "reader@test", "Content-Type": "application/json"}
    assert setup.client.get(url, headers=reader).json() is None
    assert setup.client.post(url, headers=reader, json={}).status_code == 403
    assert (
        setup.client.put(url + "/preference", headers=reader, json={"choice": "a"}).status_code
        == 403
    )
    assert (
        setup.client.put(url + "/preference", headers=JSON_ALICE, json={"choice": "a"}).status_code
        == 409
    )
    setup.conn.reply = {"status": "failed", "error": "fixture failure"}
    started = setup.client.post(url, headers=JSON_ALICE, json={})
    assert started.status_code == 200, started.text
    assert wait_comparison(setup.client, url)["state"] == "failed"
    setup.client.post(url, headers=JSON_ALICE, json={})
    assert setup.registry.advisor_calls == 2
    assert any(
        isinstance(item.data, MessageData) and item.data.role == "assistant"
        for item in setup.store.list_items(session).data
    )
    assert len(setup.launches) == 1


def test_stale_claim_is_not_replayed(setup_comparison):
    setup = setup_comparison
    session, url = create_answer(setup)
    one = append_event(
        setup.store,
        session,
        "answer",
        "alice@test",
        {"state": "pending", "started_at": 0},
        claim=True,
    )
    two = append_event(
        setup.store,
        session,
        "answer",
        "alice@test",
        {"state": "pending", "started_at": 0},
        claim=True,
    )
    assert one.id == two.id and two.deduplicated
    assert setup.client.get(url, headers=ALICE).json()["state"] == "failed"
    assert setup.client.post(url, headers=JSON_ALICE, json={}).json()["state"] == "failed"
    assert setup.registry.advisor_calls == 1


def test_text_contract_rejects_extra_context_and_old_host_outputs():
    assert text_task_eligible("Explain compound interest")
    assert not text_task_eligible("Explain this and then restart my server")
    with pytest.raises(ValueError):
        comparison_prompt({**answer_request("Explain compound interest"), "history": "private"})
    with pytest.raises(ValueError):
        parse_answer('{"candidate_id":"other","rationale":"old host"}')
    assert parse_answer('{"eligible":false,"answer":""}') is None


def test_human_execution_compares_jev_exact_choice(setup_comparison):
    from omnigent.host.frames import HostAdvisorCallFrame, decode_host_frame

    setup = setup_comparison
    _, url = create_answer(setup, human_percent=100)
    setup.conn.reply = {
        "status": "ok",
        "raw_output": json.dumps({"eligible": True, "answer": "Alternative Jev answer"}),
    }
    started = setup.client.post(url, headers=JSON_ALICE, json={})
    assert started.status_code == 200, started.text
    assert wait_comparison(setup.client, url)["state"] == "ready"
    frame = next(
        frame
        for raw in reversed(setup.registry.sent)
        if isinstance(frame := decode_host_frame(raw), HostAdvisorCallFrame)
    )
    assert frame.model == PROVIDER_GLM.model_id
    assert frame.reasoning_effort == PROVIDER_GLM.reasoning_effort
    assert frame.access_lane == "glm-direct"
    assert frame.context_session_id is None and frame.context_bridge_id is None


def test_agreement_never_compares(setup_comparison):
    setup = setup_comparison
    setup.conn.reply["raw_output"] = json.dumps(
        {"candidate_id": PROVIDER_OPENAI.choice_id, "rationale": "Same choice"}
    )
    session, url = create_answer(setup, human_percent=100)
    assert LABEL not in setup.store.get_conversation(session).labels
    assert setup.client.get(url, headers=ALICE).json() is None
    assert setup.client.post(url, headers=JSON_ALICE, json={}).json() is None
    assert setup.registry.advisor_calls == 1


def test_declined_task_stays_ineligible_without_replay(setup_comparison):
    setup = setup_comparison
    _, url = create_answer(setup)
    setup.conn.reply = {"status": "ok", "raw_output": '{"eligible":false,"answer":""}'}
    setup.client.post(url, headers=JSON_ALICE, json={})
    assert wait_comparison(setup.client, url)["state"] == "ineligible"
    setup.client.post(url, headers=JSON_ALICE, json={})
    assert setup.registry.advisor_calls == 2


def test_tool_call_response_is_not_text_comparison(setup_comparison):
    from omnigent.entities.conversation import FunctionCallData

    setup = setup_comparison
    session, url = create_answer(setup)
    setup.store.append(
        session,
        [
            NewConversationItem(
                type="function_call",
                response_id="answer",
                data=FunctionCallData(
                    call_id="tool-call", name="shell", arguments="{}", agent="fixture"
                ),
            )
        ],
    )
    assert setup.client.get(url, headers=ALICE).json() is None
    assert setup.client.post(url, headers=JSON_ALICE, json={}).json() is None
    assert setup.registry.advisor_calls == 1


@pytest.mark.parametrize("change", ["model", "effort", "prompt", "attachment", "running"])
def test_changed_or_active_execution_does_not_compare(setup_comparison, change):
    setup = setup_comparison
    session, url = create_answer(setup)
    if change in {"model", "effort"}:
        event = response_attribution_item(
            conversation_id=session,
            response_id="answer",
            requested_model=PROVIDER_GLM.model_id,
            actual_model="different-model" if change == "model" else PROVIDER_GLM.model_id,
            model_source="response_usage",
            reasoning_effort="low" if change == "effort" else None,
            advisor_round_id=setup.store.get_conversation(session).labels[
                "omnigent.advisor.round_id"
            ],
        )
        setup.store.append(session, [event.model_copy(update={"stable_id": None})])
    elif change == "running":
        setup.store.set_session_live_status(session, "running")
    else:
        content = [{"type": "input_text", "text": "Changed question"}]
        if change == "attachment":
            content.append(
                {"type": "input_image", "image_url": "https://example.invalid/image.png"}
            )
        setup.store.append(
            session,
            [
                NewConversationItem(
                    type="message", response_id="", data=MessageData(role="user", content=content)
                )
            ],
        )
    assert setup.client.get(url, headers=ALICE).json() is None
    assert setup.client.post(url, headers=JSON_ALICE, json={}).json() is None
    assert setup.registry.advisor_calls == 1
