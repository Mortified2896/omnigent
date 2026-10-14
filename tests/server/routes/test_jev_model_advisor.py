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


def test_jev_claude_selection_launches_claude_and_drops_codex_terminal_flags(db_uri):
    from types import SimpleNamespace

    from omnigent.model_advisor_provider_policy import LogicalChoice

    choice = LogicalChoice("anthropic", "claude-test", "high")
    row = {
        "model": choice.model_id,
        "accessLane": "claude-direct",
        "advisorProvider": "anthropic",
        "advisorAccessClass": "claude_plan",
        "advisorConnectionId": "claude-login",
        "advisorEntitlementKey": "claude-plan:account",
        "defaultReasoningEffort": "high",
    }
    pref = preferences()
    pref["providers"]["anthropic"] = {
        "enabled": True,
        "collapsed": False,
        "selected_choice_ids": [choice.choice_id],
        "disabled_model_ids": [],
        "transport_preference": "direct_only",
    }
    launches = []

    async def launcher(body, *, user_id, request=None):
        launches.append(body)
        return SimpleNamespace(id="conv_claude")

    client, registry = client_for(
        db_uri,
        reply={
            "status": "ok",
            "raw_output": json.dumps(
                {"candidate_id": choice.choice_id, "rationale": "Jev receipt."}
            ),
            "latency_ms": 1,
        },
        catalog_models=[*PROVIDER_CATALOG_MODELS, row],
        launcher=launcher,
    )
    with client:
        created = client.post(
            "/v1/model-advisor/rounds",
            headers=JSON_ALICE,
            json={
                "host_id": "host_1",
                "task": "Inspect this function.",
                "human_choice_id": PROVIDER_OPENAI.choice_id,
                "preferences": pref,
            },
        )
        assert created.status_code == 200, created.text
        round_id = created.json()["round_id"]
        settled = _settle(client, round_id, want_overhead=True)
        confirmed = client.post(
            f"/v1/model-advisor/rounds/{round_id}/confirm",
            headers=JSON_ALICE,
            json={
                "host_id": "host_1",
                "expected_version": settled["version"],
                "launch": {
                    "agent_id": "ag_1",
                    "workspace": "/repo",
                    "terminal_launch_args": ["--sandbox", "danger-full-access"],
                },
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["state"] == "dispatch_bound"
        assert registry.advisor_calls == 1
        assert len(launches) == 1
        assert launches[0].harness_override == "claude-native"
        assert launches[0].model_override == choice.model_id
        assert launches[0].reasoning_effort == "high"
        assert launches[0].terminal_launch_args is None
        assert launches[0].labels["omnigent.access_lane"] == "claude-direct"


@pytest.mark.parametrize("harness", ["codex-native", "claude-native"])
def test_jev_continuation_keeps_the_running_harness_even_when_model_name_is_misleading(
    db_uri, harness
):
    from types import SimpleNamespace

    from omnigent.model_advisor_provider_policy import LogicalChoice
    from tests.server.routes.test_model_advisor import _FakeConversationStore

    choice = LogicalChoice("anthropic", "claude-test", "high")
    pref = preferences()
    pref["providers"]["anthropic"] = {
        "enabled": True,
        "collapsed": False,
        "selected_choice_ids": [choice.choice_id],
        "disabled_model_ids": [],
        "transport_preference": "direct_only",
    }
    source = SimpleNamespace(
        id="conv_existing",
        host_id="host_1",
        agent_id="ag_1",
        workspace="/repo",
        kind="default",
        live_status="idle",
        labels={
            "omnigent.access_lane": "claude-direct"
            if harness == "claude-native"
            else "codex-direct"
        },
        model_override="claude-test" if harness == "claude-native" else "gpt-5.5",
        reasoning_effort="high",
        reported_model="gpt-misleading" if harness == "claude-native" else "claude-misleading",
    )
    client, registry = client_for(
        db_uri,
        catalog_models=[
            *PROVIDER_CATALOG_MODELS,
            {
                "model": choice.model_id,
                "accessLane": "claude-direct",
                "advisorProvider": "anthropic",
                "advisorAccessClass": "claude_plan",
                "advisorConnectionId": "claude-login",
                "advisorEntitlementKey": "claude-plan:account",
                "defaultReasoningEffort": "high",
            },
        ],
        conversation_store=_FakeConversationStore(source),
        session_authorizer=lambda *_: True,
        session_harness_resolver=lambda _: harness,
    )
    with client:
        preview = client.post(
            "/v1/model-advisor/prompt",
            headers=JSON_ALICE,
            json={
                "host_id": "host_1",
                "task": "Continue this chat.",
                "continue_session_id": source.id,
                "human_choice_id": choice.choice_id
                if harness == "claude-native"
                else PROVIDER_OPENAI.choice_id,
                "preferences": pref,
            },
        )
        assert preview.status_code == 200, preview.text
        prompt = json.loads(preview.json()["prompt"])
        expected = (
            {choice.choice_id}
            if harness == "claude-native"
            else {PROVIDER_OPENAI.choice_id, PROVIDER_GLM.choice_id}
        )
        assert set(prompt["questions"]["route"]["criteria"]) == expected
        assert registry.advisor_calls == 0
