"""The generic current-model option uses only an authorized session's context."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from omnigent.host.frames import (
    HostAdvisorCallFrame,
    HostAdvisorCallResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from tests.server.routes.test_model_advisor import (
    JSON_ALICE,
    PROVIDER_CATALOG_MODELS,
    PROVIDER_GLM,
    PROVIDER_OPENAI,
    _FakeConversationStore,
    _settle,
    client_for,
)


def preferences(choice):
    return {
        "schema_version": 3,
        "enabled": True,
        "providers": {
            provider: {
                "enabled": provider == choice.provider,
                "collapsed": False,
                "selected_choice_ids": [choice.choice_id] if provider == choice.provider else [],
                "disabled_model_ids": [],
                "transport_preference": "omniroute_preferred",
            }
            for provider in ("openai", "glm")
        },
        "advisor_choice_id": None,
        "advisor_model_mode": "current",
        "advisor_reasoning_effort": choice.reasoning_effort,
        "human_probability_percent": 0,
        "unresolved_legacy_ids": [],
        "route_review_required": [],
    }


@pytest.mark.parametrize(
    "choice,lane", [(PROVIDER_OPENAI, "codex-direct"), (PROVIDER_GLM, "glm-direct")]
)
@pytest.mark.parametrize("context_ack", ["conv_current", None, "different-chat"])
@pytest.mark.parametrize("with_attachments", [False, True])
def test_current_model_round_inherits_owned_running_chat_and_retains_its_connection(
    db_uri, choice, lane, context_ack, with_attachments
):
    conversation = SimpleNamespace(
        id="conv_current",
        host_id="host_1",
        agent_id="ag_1",
        workspace="/repo",
        kind="default",
        live_status="running",
        labels={"omnigent.access_lane": lane, "omnigent.codex_native.bridge_id": "bridge-current"},
        model_override=None,
        reported_model=choice.model_id,
        reasoning_effort=choice.reasoning_effort,
    )
    from omnigent.model_advisor_attachments import (
        advisor_attachment_context,
        normalize_attachments,
    )
    from omnigent.model_advisor_workflow import document_digest

    attachments = [{"filename": "notes.txt", "content_type": "text/plain", "data": "aGVsbG8="}]
    digest = (
        document_digest(advisor_attachment_context(normalize_attachments(attachments)))
        if with_attachments
        else None
    )
    store = _FakeConversationStore(conversation)
    client, registry = client_for(
        db_uri,
        catalog_models=PROVIDER_CATALOG_MODELS,
        conversation_store=store,
        session_authorizer=lambda user, sid: user == "alice@test" and sid == conversation.id,
        reply=asdict(
            decode_host_frame(
                encode_host_frame(
                    HostAdvisorCallResultFrame(
                        request_id="context-advice",
                        status="ok",
                        raw_output=json.dumps(
                            {"candidate_id": choice.choice_id, "rationale": "Use this effort"}
                        ),
                        input_tokens=10000,
                        cached_input_tokens=9000,
                        context_session_id=context_ack,
                        attachment_digest=digest,
                    )
                )
            )
        ),
    )
    with client:
        saved = client.put(
            "/v1/model-advisor/preferences",
            headers=JSON_ALICE,
            json={
                "host_id": "host_1",
                "expected_version": 0,
                "preferences": preferences(choice),
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["logical_preferences"]["advisor_model_mode"] == "current"
        body = {
            "host_id": "host_1",
            "task": "Next task",
            "human_choice_id": choice.choice_id,
            "continue_session_id": conversation.id,
            "submission_key": "current-round",
            **({"attachments": attachments} if with_attachments else {}),
        }
        rejected = client.post(
            "/v1/model-advisor/rounds",
            headers=JSON_ALICE,
            json={**body, "continue_session_id": "someone-elses-chat"},
        )
        assert rejected.status_code == 403
        created = client.post("/v1/model-advisor/rounds", headers=JSON_ALICE, json=body)
        assert created.status_code == 200, created.text
        result = _settle(
            client, created.json()["round_id"], want_overhead=context_ack == conversation.id
        )
        if context_ack != conversation.id:
            assert result["state"] == "blocked"
            assert "Update the connected host" in result["failure_reason"]
            assert not store.updates
            return
        assert result["state"] == "awaiting_confirmation"
        frame = next(
            decode_host_frame(raw)
            for raw in registry.sent
            if isinstance(decode_host_frame(raw), HostAdvisorCallFrame)
        )
        assert (frame.context_session_id, frame.context_bridge_id) == (
            conversation.id,
            "bridge-current",
        )
        if with_attachments:
            assert frame.request["attachments"][0]["text"] == "hello"
        assert frame.model == choice.model_id
        assert frame.access_lane == lane
        assert frame.reasoning_effort == choice.reasoning_effort
        assert "inherited context" in frame.request["instructions"]
        assert "bridge-current" not in json.dumps(frame.request)
        assert result["decision_context"]["advisor_executor_model_id"] == choice.model_id
        # A completed recommendation cannot mutate an active parent turn.
        confirmation = client.post(
            f"/v1/model-advisor/rounds/{result['round_id']}/confirm",
            headers=JSON_ALICE,
            json={
                "host_id": "host_1",
                "expected_version": result["version"],
                "launch": {
                    "agent_id": "ag_1",
                    "workspace": "/repo",
                    "continue_session_id": conversation.id,
                },
            },
        )
        assert confirmation.status_code == 409
        assert not store.updates
        retained = client.get(
            f"/v1/model-advisor/rounds/{result['round_id']}?host_id=host_1",
            headers=JSON_ALICE,
        ).json()
        assert retained["state"] == "awaiting_confirmation"
        assert retained["version"] == result["version"]


def test_current_model_new_chat_uses_execution_pick_without_claiming_existing_context(db_uri):
    client, registry = client_for(
        db_uri,
        catalog_models=PROVIDER_CATALOG_MODELS,
        reply={
            "status": "ok",
            "raw_output": json.dumps(
                {"candidate_id": PROVIDER_OPENAI.choice_id, "rationale": "Enough"}
            ),
        },
    )
    with client:
        response = client.post(
            "/v1/model-advisor/rounds",
            headers=JSON_ALICE,
            json={
                "host_id": "host_1",
                "task": "First task",
                "human_choice_id": PROVIDER_OPENAI.choice_id,
                "preferences": preferences(PROVIDER_OPENAI),
            },
        )
        assert response.status_code == 200, response.text
        result = _settle(client, response.json()["round_id"])
        assert result["state"] == "awaiting_confirmation"
        frame = next(
            decode_host_frame(raw)
            for raw in registry.sent
            if isinstance(decode_host_frame(raw), HostAdvisorCallFrame)
        )
        assert frame.context_session_id is None and frame.context_bridge_id is None
        assert result["decision_context"]["advisor_executor_model_id"] == PROVIDER_OPENAI.model_id
