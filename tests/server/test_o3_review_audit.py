"""Reviewer capture, safe persistence and legacy rollback compatibility."""

from __future__ import annotations

import json
from typing import cast

import pytest

from omnigent.server.o3_routing_review.adviser import OmniRouteRoutingAdviser, ReviewerCaptureError
from omnigent.server.o3_routing_review.audit import redact, snapshot
from omnigent.server.o3_routing_review.models import (
    ProposalAdjustmentRequest,
    ProposalCreateRequest,
)
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteResponse
from omnigent.server.o3_routing_review.registry import BenchmarkRegistry, DifficultyCalibration
from omnigent.server.o3_routing_review.store import ProposalStore
from tests.server.test_o3_routing_review import _SLICE, _analysis, _candidate, _evidence, _service


@pytest.mark.asyncio
async def test_complete_exchange_survives_parse_reload_and_legacy_read(tmp_path):
    output = {
        **_analysis().model_dump(mode="json"),
        "notes": "Uncertainty",
        "arbitrary": [1, {"unknown": True}],
    }
    body = {
        "output_text": json.dumps(output),
        "model": "observed-model",
        "reasoning": {"effort": "high"},
        "output": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Returned rationale"}],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 20},
        "extra": {"future_field": "retained"},
    }
    raw = json.dumps(body, indent=3) + "\n"

    class Client:
        async def create_response(self, request):
            self.request = request
            return OmniRouteResponse(
                body, {"x-provider": "test", "set-cookie": "session=secret"}, raw
            )

    client = Client()
    adviser = OmniRouteRoutingAdviser(cast(OmniRouteClient, client))
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(_candidate())],
        candidates=[],
        calibration=DifficultyCalibration(
            {
                "calibration_version": "test-v1",
                "calibrations": [
                    {
                        "benchmark_id": _SLICE.benchmark_id,
                        "version": _SLICE.version,
                        "slice_id": _SLICE.slice_id,
                        "thresholds": {"normal": 0.5},
                    }
                ],
            }
        ),
    )
    analysis = await adviser.analyse(
        prompt="Exact task",
        workspace_summary="Exact workspace",
        registry=registry,
        candidates=[],
        model="requested-combo",
        reasoning_effort="medium",
    )
    exchange = analysis._exchanges[0]
    assert exchange.request == client.request
    assert exchange.response_text == raw
    assert exchange.response == body
    assert exchange.response_headers["set-cookie"] == "[REDACTED]"
    assert exchange.reasoning_summary == "Returned rationale"
    assert exchange.transmitted_model == "requested-combo"
    assert exchange.actual_model == "observed-model"
    assert exchange.actual_provider == "test"
    assert exchange.transmitted_effort == "medium"
    assert exchange.observed_effort == "high"
    assert exchange.duration_ms is not None
    assert exchange.parsed["difficulty"] == "normal"
    assert "arbitrary" not in exchange.parsed
    service, _ = _service(tmp_path, registry, [_candidate()], analysis)
    proposal = await service.create_proposal(ProposalCreateRequest(prompt="Exact task"))
    loaded = ProposalStore(service.store.path).get(proposal.proposal_id)
    assert loaded.adviser_exchanges[0].response_text == raw
    assert loaded.audit["initial_constraints"] == proposal.approved_constraints.model_dump(
        mode="json"
    )
    assert loaded.audit["history"][0]["state"]["evaluations"]
    state = json.loads(service.store.path.read_text())
    legacy = state["proposals"][proposal.proposal_id]
    assert "audit" not in legacy
    assert "response" not in legacy["adviser_exchanges"][0]
    assert state["proposal_extensions"][proposal.proposal_id]
    assert ProposalStore._parse(legacy).adviser_exchanges[0].response is None
    assert service.store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_all_malformed_attempts_are_persisted_without_invented_recommendation(tmp_path):
    class Client:
        async def create_response(self, request):
            return OmniRouteResponse(
                {"output_text": "{ broken", "notes": "still safe"}, {}, "{ broken raw transport"
            )

    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[], candidates=[])
    service, _ = _service(tmp_path, registry, [], _analysis())
    service.adviser = OmniRouteRoutingAdviser(cast(OmniRouteClient, Client()))
    with pytest.raises(ReviewerCaptureError) as error:
        await service.create_proposal(ProposalCreateRequest(prompt="Test malformed output"))
    capture = ProposalStore(service.store.path).get_failed_review(error.value.review_id)
    assert len(capture["exchanges"]) == 2
    assert all(
        e["response_text"] == "{ broken raw transport" and e["parsed"] is None
        for e in capture["exchanges"]
    )
    assert service.store.list() == []


@pytest.mark.parametrize(
    "value",
    [
        {"Authorization": "Bearer hidden", "cookies": "private", "api_key": "secret"},
        {"text": 'Authorization: Bearer abc123\nAPI_KEY="secret-key" password=private'},
        {"nested": [{"access_token": "token-value", "usage": {"input_tokens": 123}}]},
    ],
)
def test_redaction_preserves_safe_metadata(value):
    serialized = json.dumps(redact(value))
    assert not any(
        secret in serialized
        for secret in ("hidden", "private", "secret-key", "token-value", "abc123")
    )
    if "usage" in json.dumps(value):
        assert "123" in serialized


def test_redacts_known_environment_secrets_and_preserves_raw_whitespace(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "test-sensitive-value")
    assert redact("  first\nlast  ") == "  first\nlast  "
    assert redact("prefix test-sensitive-value suffix") == "prefix [REDACTED] suffix"


@pytest.mark.asyncio
async def test_revisions_keep_original_and_every_candidate_reason(tmp_path):
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(_candidate())],
        candidates=[],
        calibration=DifficultyCalibration(
            {
                "calibration_version": "test-v1",
                "calibrations": [
                    {
                        "benchmark_id": _SLICE.benchmark_id,
                        "version": _SLICE.version,
                        "slice_id": _SLICE.slice_id,
                        "thresholds": {"normal": 0.5},
                    }
                ],
            }
        ),
    )
    service, _ = _service(tmp_path, registry, [_candidate()], _analysis())
    p = await service.create_proposal(ProposalCreateRequest(prompt="Inspect"))
    original = p.audit["initial_constraints"]
    changed = await service.adjust_proposal(
        p.proposal_id,
        ProposalAdjustmentRequest(minimum_score=0.72, requirement_overrides={"tools": False}),
    )
    loaded = service.store.get(p.proposal_id)
    assert loaded.audit["initial_constraints"] == original
    assert loaded.approved_constraints.benchmark.minimum_score == 0.72
    assert len(loaded.audit["history"]) == 2
    assert (
        loaded.audit["history"][0]["state"]["approved_constraints"]["benchmark"]["minimum_score"]
        != 0.72
    )
    snapshot(changed, "unchanged")
    assert len(changed.audit["history"]) == 2


@pytest.mark.asyncio
async def test_transport_captures_non_json_and_redacts_credentials(respx_mock):
    import httpx

    from omnigent.server.o3_routing_review.omniroute import OmniRouteError

    route = respx_mock.post("http://127.0.0.1:20128/v1/responses").mock(
        return_value=httpx.Response(
            502,
            text="bad response API_KEY=private-value",
            headers={"set-cookie": "private-cookie"},
        )
    )
    client = OmniRouteClient("http://127.0.0.1:20128", "transport-secret")
    with pytest.raises(OmniRouteError) as exc:
        await client.create_response({"model": "test-model", "input": "hello"})
    assert route.called
    assert exc.value.response.raw_text == "bad response API_KEY=[REDACTED]"
    assert exc.value.response.headers["set-cookie"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_execution_preserves_fallback_facts_without_assuming_observed_effort():
    from datetime import datetime, timezone

    record = OmniRouteClient._sanitize_execution(
        {
            "id": "call",
            "path": "/v1/responses",
            "method": "POST",
            "sessionTag": "s",
            "provider": "second",
            "model": "B",
            "status": 200,
            "requestedModel": "A",
        },
        {"requestBody": {"reasoning": {"effort": "high"}}, "fallbackReason": "A unavailable"},
        timestamp=datetime.now(timezone.utc),
    )
    assert record.transport["requested_model"] == "A"
    assert record.transport["observed_model"] == "B"
    assert record.transport["requested_effort"] == "high"
    assert record.transport["observed_effort"] is None
    assert record.transport["fallbackReason"] == "A unavailable"


@pytest.mark.asyncio
async def test_old_release_updates_cannot_attach_stale_extensions(tmp_path):
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[], candidates=[])
    service, _ = _service(tmp_path, registry, [], _analysis())
    p = await service.create_proposal(ProposalCreateRequest(prompt="inspect"))
    state = json.loads(service.store.path.read_text())
    assert state["proposal_extensions"][p.proposal_id]["fields"]
    state["proposals"][p.proposal_id]["decision_reason"] = "Changed on older release"
    service.store.path.write_text(json.dumps(state))
    loaded = service.store.get(p.proposal_id)
    assert loaded.audit is None
    assert loaded.decision_reason == "Changed on older release"
    assert json.loads(service.store.path.read_text())["proposal_extensions"][p.proposal_id][
        "fields"
    ]
