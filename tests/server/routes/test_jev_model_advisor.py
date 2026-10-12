"""Jev routing uses the same authenticated, exactly-once round/launch flow."""

import json

import pytest

from omnigent.host.frames import HostAdvisorCallFrame, decode_host_frame
from omnigent.host.jev_router import JEV_ACCESS_LANE, JEV_MODEL
from tests.server.routes.test_model_advisor import (
    JSON_ALICE,
    PROVIDER_CATALOG_MODELS,
    PROVIDER_GLM,
    PROVIDER_OPENAI,
    _settle,
    client_for,
)


def preferences():
    return {
        "schema_version": 3,
        "enabled": True,
        "providers": {
            provider: {
                "enabled": True,
                "collapsed": False,
                "selected_choice_ids": [choice.choice_id],
                "disabled_model_ids": [],
                "transport_preference": "omniroute_preferred",
            }
            for provider, choice in (("openai", PROVIDER_OPENAI), ("glm", PROVIDER_GLM))
        },
        "advisor_model_mode": "jev",
        "advisor_choice_id": "unavailable-remembered-advisor",
        "human_probability_percent": 0,
        "unresolved_legacy_ids": [],
        "route_review_required": [],
    }


@pytest.mark.parametrize("valid", [True, False])
def test_jev_round_dispatches_once_and_rejects_outside_settings_choices(db_uri, valid):
    reply = {
        "status": "ok",
        "raw_output": json.dumps(
            {
                "candidate_id": PROVIDER_GLM.choice_id if valid else "outside-settings",
                "rationale": "Jev Router selected this Settings choice from the current prompt.",
            }
        ),
        "latency_ms": 100,
    }
    client, registry = client_for(db_uri, reply=reply, catalog_models=PROVIDER_CATALOG_MODELS)
    with client:
        saved = client.put(
            "/v1/model-advisor/preferences",
            headers=JSON_ALICE,
            json={
                "host_id": "host_1",
                "preferences": preferences(),
                "expected_version": 0,
            },
        )
        assert saved.status_code == 200, saved.text
        body = {
            "host_id": "host_1",
            "task": "Explain this function.",
            "human_choice_id": PROVIDER_OPENAI.choice_id,
            "submission_key": "jev-submission",
        }
        preview = client.post("/v1/model-advisor/prompt", headers=JSON_ALICE, json=body)
        assert preview.status_code == 200, preview.text
        prompt = json.loads(preview.json()["prompt"])
        assert prompt["model"] == JEV_MODEL
        assert prompt["state"] == body["task"]
        assert registry.advisor_calls == 0
        created = client.post("/v1/model-advisor/rounds", headers=JSON_ALICE, json=body)
        assert created.status_code == 200, created.text
        round_id = created.json()["round_id"]
        settled = _settle(client, round_id, want_overhead=valid)
        assert settled["state"] == ("awaiting_confirmation" if valid else "blocked")
        client.post("/v1/model-advisor/rounds", headers=JSON_ALICE, json=body)
        assert registry.advisor_calls == 1
        frames = [decode_host_frame(raw) for raw in registry.sent]
        frame = next(frame for frame in frames if isinstance(frame, HostAdvisorCallFrame))
        assert frame.access_lane == JEV_ACCESS_LANE and frame.model == JEV_MODEL
        assert frame.context_session_id is None and frame.reasoning_effort is None
        assert set(frame.request) == {"instructions", "task", "candidates"}
        assert {row["candidate_id"] for row in frame.request["candidates"]} == {
            PROVIDER_OPENAI.choice_id,
            PROVIDER_GLM.choice_id,
        }
        if valid:
            assert settled["review"]["assigned_choice_id"] == PROVIDER_GLM.choice_id
            assert settled["decision_context"]["advisor_executor_model_id"] == JEV_MODEL
