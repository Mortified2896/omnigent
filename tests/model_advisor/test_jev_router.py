"""Jev's outbound contract and response boundary, with no paid provider calls."""

import json
from dataclasses import replace

import httpx
import pytest

from omnigent.host.advisor_call import AdvisorCallError
from omnigent.host.jev_router import (
    JEV_ENDPOINT,
    JEV_MODEL,
    build_jev_request,
    generate_jev_selection,
    parse_jev_response,
)
from omnigent.model_advisor_provider_policy import ProviderPreferences
from omnigent.model_advisor_provider_workflow import (
    LogicalAdvisorError,
    LogicalFrozenRound,
    freeze_logical_round,
)
from tests.model_advisor.test_provider_workflow import CATALOG, GLM_HIGH, OPENAI_LOW, PREFERENCES


def jev_round():
    frozen = freeze_logical_round(
        owner_id="owner",
        host_id="host",
        round_id="jev-round",
        settings_revision="settings-1",
        catalog_revision="catalog-1",
        task="Explain this function.",
        preferences=replace(
            PREFERENCES, advisor_model_mode="jev", advisor_choice_id="stale-choice"
        ),
        catalog=CATALOG,
        routes_by_choice={},
        human_choice_id=OPENAI_LOW.choice_id,
    )
    return replace(
        frozen,
        current_execution={"model_id": "private-current", "reasoning_effort": "max"},
        continuation_session_id="private-session",
        task_tags=("private-tag",),
        attachments=({"filename": "private.txt", "text": "private-attachment"},),
    )


def test_jev_freezes_only_enabled_choices_without_a_generative_advisor():
    frozen = jev_round()
    assert frozen.advisor is None and frozen.advisor_transport is None
    assert LogicalFrozenRound.from_payload(frozen.to_payload()) == frozen
    assert (
        ProviderPreferences.from_payload(
            frozen.preferences_snapshot.to_payload()
        ).advisor_model_mode
        == "jev"
    )
    body = build_jev_request(frozen.advisor_input())
    assert body["model"] == JEV_MODEL
    assert body["state"] == frozen.task
    assert set(body["questions"]["route"]["criteria"]) == {
        OPENAI_LOW.choice_id,
        GLM_HIGH.choice_id,
    }
    for forbidden in ("private-", "human_choice", "transport", "approval", "stale-choice"):
        assert forbidden not in json.dumps(body)
    assert frozen.decision_context_payload()["advisor_executor_model_id"] == JEV_MODEL
    assert frozen.decision_context_payload()["advisor_executor_choice_id"] is None
    with pytest.raises(LogicalAdvisorError, match="generative advisor"):
        replace(frozen, preferences_snapshot=PREFERENCES)


def response(criteria):
    return {
        "answers": {
            "route": {
                "type": "choice",
                "choice": OPENAI_LOW.choice_id,
                "confidence": 0.8,
                "probabilities": {
                    key: 0.75 if key == OPENAI_LOW.choice_id else 0.25 for key in criteria
                },
            }
        },
        "id": "decision-1",
        "usage": {"input_tokens": 200, "output_tokens": 20},
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda answer: answer.update(choice="outside-pool"),
        lambda answer: answer.update(confidence=float("nan")),
        lambda answer: answer.update(confidence=True),
        lambda answer: answer.update(probabilities={}),
        lambda answer: answer["probabilities"].update({"extra-choice": 0.1}),
        lambda answer: answer.update(type="score"),
    ],
)
def test_jev_rejects_outside_choices_and_invalid_probabilities(mutation):
    criteria = build_jev_request(jev_round().advisor_input())["questions"]["route"]["criteria"]
    payload = response(criteria)
    mutation(payload["answers"]["route"])
    with pytest.raises(AdvisorCallError):
        parse_jev_response(payload, criteria)


async def test_one_http_decision_uses_host_key_and_never_sends_other_context(monkeypatch):
    request = jev_round().advisor_input()
    body = build_jev_request(request)
    calls = []

    def handler(req):
        calls.append(req)
        assert str(req.url) == JEV_ENDPOINT
        assert req.headers["authorization"] == "Bearer fixture-key"
        assert json.loads(req.content) == body
        return httpx.Response(200, json=response(body["questions"]["route"]["criteria"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("omnigent.host.jev_router.httpx.AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr("omnigent.host.jev_router._openrouter_key", lambda: "fixture-key")
    result = await generate_jev_selection(request=request)
    assert len(calls) == 1
    assert result.input_tokens == 200 and result.output_tokens == 20
    assert result.cached_input_tokens is None
    assert result.response_id == "decision-1"
    parsed = json.loads(result.raw_output)
    assert parsed["candidate_id"] == OPENAI_LOW.choice_id
    assert "confidence 80.0%" in parsed["rationale"]


@pytest.mark.parametrize("status", [401, 429, 503])
async def test_http_failures_do_not_retry_or_expose_response_bodies(monkeypatch, status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, text="fixture-private-key prompt provider error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("omnigent.host.jev_router.httpx.AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr("omnigent.host.jev_router._openrouter_key", lambda: "fixture-key")
    with pytest.raises(AdvisorCallError, match=f"HTTP {status}") as exc:
        await generate_jev_selection(request=jev_round().advisor_input())
    assert "fixture-private" not in str(exc.value)
    assert len(calls) == 1


def test_missing_host_key_has_a_configuration_error(monkeypatch):
    monkeypatch.setattr(
        "omnigent.util.env_credentials.getenv_with_omnigent_prefix", lambda name: None
    )
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    from omnigent.host.jev_router import _openrouter_key

    with pytest.raises(AdvisorCallError, match="OpenRouter API key on this host"):
        _openrouter_key()


async def test_host_dispatches_jev_without_starting_codex_or_inheriting_session(monkeypatch):
    from omnigent.host.advisor_call import AdvisorCallResult
    from omnigent.host.connect import HostProcess
    from omnigent.host.frames import HostAdvisorCallFrame
    from omnigent.host.jev_router import JEV_ACCESS_LANE

    calls = []

    async def decide(*, request):
        calls.append(request)
        return AdvisorCallResult(raw_output="fixture", latency_ms=15)

    async def unexpected(**kwargs):
        raise AssertionError("Jev must not start a generative advisor")

    monkeypatch.setattr("omnigent.host.jev_router.generate_jev_selection", decide)
    monkeypatch.setattr("omnigent.host.advisor_call.generate_advisor_selection", unexpected)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.advisor.generate_context_advisor_selection", unexpected
    )
    frame = HostAdvisorCallFrame(
        request_id="jev-host",
        request=jev_round().advisor_input(),
        model=JEV_MODEL,
        access_lane=JEV_ACCESS_LANE,
        reasoning_effort=None,
    )
    host = object.__new__(HostProcess)
    result = await host._handle_advisor_call(frame)
    assert result.status == "ok" and result.raw_output == "fixture"
    assert len(calls) == 1
    result = await host._handle_advisor_call(replace(frame, context_session_id="private-parent"))
    assert result.status == "failed"
    assert len(calls) == 1


@pytest.fixture(autouse=True)
def isolate_jev_credentials(monkeypatch):
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda name: None)
    monkeypatch.setattr("omnigent.host.jev_router._opencode_key", lambda: None)


async def test_opencode_uses_zen_endpoint_and_keeps_exact_settings_choices(monkeypatch):
    from omnigent.host.jev_router import JEV_ZEN_ENDPOINT, JEV_ZEN_MODEL

    body = build_jev_request(jev_round().advisor_input())
    calls = []

    def handler(req):
        calls.append(req)
        assert str(req.url) == JEV_ZEN_ENDPOINT
        assert req.headers["authorization"] == "Bearer fixture-zen-key"
        posted = json.loads(req.content)
        assert posted == {**body, "model": JEV_ZEN_MODEL}
        return httpx.Response(200, json=response(body["questions"]["route"]["criteria"]))

    def forbidden():
        raise AssertionError("A Zen request must not resolve an OpenRouter key")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("omnigent.host.jev_router.httpx.AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr("omnigent.host.jev_router._opencode_key", lambda: "fixture-zen-key")
    monkeypatch.setattr("omnigent.host.jev_router._openrouter_key", forbidden)
    result = await generate_jev_selection(request=jev_round().advisor_input())
    assert len(calls) == 1
    assert json.loads(result.raw_output)["candidate_id"] == OPENAI_LOW.choice_id


def test_openrouter_reads_dedicated_stored_key(monkeypatch):
    from omnigent.host.jev_router import _openrouter_key

    monkeypatch.setattr(
        "omnigent.util.env_credentials.getenv_with_omnigent_prefix", lambda name: None
    )
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "fixture-key" if name == "jev-openrouter" else None,
    )
    assert _openrouter_key() == "fixture-key"


def test_jev_selects_concrete_harness_model_pairs_and_preserves_old_preferences():
    from omnigent.model_advisor_provider_policy import LogicalChoice, ProviderSelection

    claude = LogicalChoice("anthropic", "claude-test", "high")
    preferences = replace(
        PREFERENCES,
        advisor_model_mode="jev",
        anthropic=ProviderSelection(
            enabled=True,
            selected_choice_ids=(claude.choice_id,),
            transport_preference="direct_only",
        ),
    )
    assert "anthropic" not in PREFERENCES.to_payload()["providers"]
    assert ProviderPreferences.from_payload(preferences.to_payload()) == preferences
    frozen = freeze_logical_round(
        owner_id="owner",
        host_id="host",
        round_id="harness-round",
        settings_revision="settings",
        catalog_revision="catalog",
        task="Inspect this project.",
        preferences=preferences,
        catalog=(*CATALOG, claude),
        routes_by_choice={},
        human_choice_id=OPENAI_LOW.choice_id,
    )
    assert LogicalFrozenRound.from_payload(frozen.to_payload()) == frozen
    body = build_jev_request(frozen.advisor_input())
    criteria = body["questions"]["route"]["criteria"]
    assert "Harness: claude-native" in criteria[claude.choice_id]
    assert "Harness: codex-native" in criteria[OPENAI_LOW.choice_id]
    assert set(criteria) == {OPENAI_LOW.choice_id, GLM_HIGH.choice_id, claude.choice_id}
