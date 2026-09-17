"""Independent forecasts are durable before dispatch without changing manual settings."""

import json

import pytest

from omnigent.server import o3_success_forecast as o3
from omnigent.server.schemas import SessionEventInput
from omnigent.server.task_experiment import list_experiment_events


@pytest.mark.asyncio
async def test_independent_commit_and_retry(conversation_store, monkeypatch, tmp_path):
    store = conversation_store
    conv = store.create_conversation()
    conv.model_override = "codex/example"
    conv.reasoning_effort = "low"
    candidate = {"canonical_model": "example", "compute_profile": "low"}
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"schema_version": 1, "candidates": [candidate]}))
    monkeypatch.setenv("OMNIGENT_O3_SUCCESS_FORECAST", "1")
    monkeypatch.setenv("OMNIGENT_O3_SUCCESS_CANDIDATES", str(path))
    monkeypatch.setenv("OMNIGENT_O3_SUCCESS_ADVISER_MODEL", "test-adviser")
    calls = []

    async def predict(inputs, adviser):
        assert list_experiment_events(store, conv.id)[0]["human_probability"] == 78
        assert set(inputs) == {
            "task",
            "task_truncated",
            "selected",
            "candidates",
            "benchmark_evidence",
        }
        assert "78" not in json.dumps(inputs)
        calls.append(inputs)
        return o3.Prediction(
            probability=65,
            category="test",
            alternative=None,
            rationale="Uncalibrated test estimate",
        ), {}

    monkeypatch.setattr(o3, "predict", predict)
    body = SessionEventInput(
        type="message",
        success_forecast={"probability": 78},
        data={
            "role": "user",
            "stable_id": "a" * 32,
            "content": [{"type": "input_text", "text": "test task"}],
        },
    )
    await o3.commit_attempt(store, conv, body, "alice", "codex-native")
    await o3.commit_attempt(store, conv, body, "alice", "codex-native")
    rows = list_experiment_events(store, conv.id)
    assert [r["kind"] for r in rows] == ["forecast", "o3_shadow"]
    assert rows[1]["probability"] == 65
    assert rows[1]["forecaster_id"] == o3.FORECASTER_ID
    assert rows[1]["human_probability_provided"] is False
    assert rows[1]["calibrated"] is False
    assert len(calls) == 1
    assert conv.model_override == "codex/example" and conv.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_unknown_configuration_does_not_invent_forecast(conversation_store, monkeypatch):
    monkeypatch.setenv("OMNIGENT_O3_SUCCESS_FORECAST", "1")
    monkeypatch.delenv("OMNIGENT_O3_SUCCESS_CANDIDATES", raising=False)
    conv = conversation_store.create_conversation()
    body = SessionEventInput(
        type="message",
        success_forecast={"probability": None},
        data={
            "role": "user",
            "stable_id": "b" * 32,
            "content": [{"type": "input_text", "text": "task"}],
        },
    )
    await o3.commit_attempt(conversation_store, conv, body, "alice")
    shadow = list_experiment_events(conversation_store, conv.id)[-1]
    assert shadow["status"] == "unavailable"
    assert shadow["probability"] is None
    assert shadow["alternative"] is None


@pytest.mark.asyncio
async def test_disabled_has_no_shadow_or_provider_call(conversation_store, monkeypatch):
    monkeypatch.delenv("OMNIGENT_O3_SUCCESS_FORECAST", raising=False)
    conv = conversation_store.create_conversation()
    body = SessionEventInput(
        type="message",
        success_forecast={"probability": None},
        data={
            "role": "user",
            "stable_id": "c" * 32,
            "content": [{"type": "input_text", "text": "task"}],
        },
    )
    await o3.commit_attempt(conversation_store, conv, body, "alice")
    assert [r["kind"] for r in list_experiment_events(conversation_store, conv.id)] == ["forecast"]


def test_profiles_remain_distinct():
    assert o3.Candidate(canonical_model="example", compute_profile="none") != o3.Candidate(
        canonical_model="example", compute_profile="high"
    )
