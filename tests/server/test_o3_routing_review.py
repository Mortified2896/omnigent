"""Focused policy and lifecycle tests for the opt-in O3 routing review."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.o3_routing_review.adviser import OmniRouteRoutingAdviser, RoutingAdviser
from omnigent.server.o3_routing_review.evaluator import evaluate_candidates
from omnigent.server.o3_routing_review.models import (
    AdviserAnalysis,
    ApprovedConstraints,
    BenchmarkEvidence,
    BenchmarkRequirement,
    CandidateEvaluation,
    CandidateProfile,
    CandidateSnapshot,
    CandidateStatus,
    DecisionAction,
    DecompositionItem,
    Disposition,
    EvidenceClass,
    EvidencePolicy,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalOutcomeRequest,
    RoutingProposal,
    RoutingRequirements,
)
from omnigent.server.o3_routing_review.omniroute import (
    OmniRouteClient,
    OmniRouteError,
    OmniRouteResponse,
)
from omnigent.server.o3_routing_review.registry import BenchmarkRegistry, default_slices
from omnigent.server.o3_routing_review.routes import create_o3_routing_review_router
from omnigent.server.o3_routing_review.service import (
    O3RoutingReviewService,
    RoutingReviewError,
)
from omnigent.server.o3_routing_review.store import ProposalStore

_SLICE = default_slices()[0]


def _requirement(*, minimum: float = 0.5) -> BenchmarkRequirement:
    return BenchmarkRequirement(
        benchmark_id=_SLICE.benchmark_id,
        version=_SLICE.version,
        slice_id=_SLICE.slice_id,
        minimum_score=minimum,
        reason="task needs terminal and tool competence",
    )


def _candidate(
    candidate_id: str = "codex-low",
    *,
    provider_id: str = "codex",
    model: str = "gpt-5.5",
    effort: str = "low",
    **updates: object,
) -> CandidateSnapshot:
    values: dict[str, object] = {
        "candidate_id": candidate_id,
        "provider_id": provider_id,
        "model": model,
        "catalogue_model_id": f"{provider_id}/{model}",
        "harness": "codex-native",
        "supported_reasoning_efforts": [effort],
        "context_tokens": 128_000,
        "terminal": True,
        "tools": True,
        "vision": False,
        "responses_api": True,
        "monetary_cost_usd": None,
        "cost_source": "test fixture",
        "quota_source": "test fixture",
        "last_full_probe_at": "2026-09-03T00:00:00+08:00",
        "probe_reference": "test full Responses/tool-continuation probe",
        "provider_usable": True,
        "model_present": True,
        "quota_available": True,
        "quota_remaining_percent": 80,
        "quota_reset_at": None,
        "recent_success_rate": 0.99,
        "recent_retry_rate": 0.01,
        "latency_ms": 100,
    }
    values.update(updates)
    return CandidateSnapshot.model_validate(values)


def _evidence(
    candidate: CandidateSnapshot,
    *,
    evidence_class: EvidenceClass = EvidenceClass.EXACT,
    point: float = 0.7,
    lower: float | None = 0.6,
    effort: str = "low",
) -> BenchmarkEvidence:
    return BenchmarkEvidence(
        benchmark_id=_SLICE.benchmark_id,
        benchmark_version=_SLICE.version,
        slice_id=_SLICE.slice_id,
        task_manifest_digest=_SLICE.task_manifest_digest,
        harness=candidate.harness,
        harness_version="test-harness",
        model=candidate.model,
        provider_path=candidate.provider_id,
        reasoning_effort=effort,
        point_score=point,
        confidence_lower=lower,
        confidence_upper=0.8,
        number_of_tasks=10,
        number_of_attempts=10,
        evidence_class=evidence_class,
        source_type="authoritative_test_artifact",
        source_reference="test://tb4/result",
        evaluation_date="2026-09-03",
    )


def _analysis(
    *,
    minimum: float = 0.5,
    effort: str = "low",
    evidence_policy: EvidencePolicy = EvidencePolicy.STRICT,
    requirements: RoutingRequirements | None = None,
    decomposition: list[DecompositionItem] | None = None,
) -> AdviserAnalysis:
    return AdviserAnalysis(
        task_summary="Inspect a harmless repository state",
        task_classification="systems",
        difficulty="medium",
        risk="low",
        requirements=requirements or RoutingRequirements(),
        benchmark_requirements=[_requirement(minimum=minimum)],
        proposed_reasoning_effort=effort,
        evidence_policy=evidence_policy,
        disposition=Disposition.ROUTE,
        confidence=0.8,
        rationale="A terminal-capable model is required.",
        decomposition=decomposition or [],
    )


def _constraints(analysis: AdviserAnalysis) -> ApprovedConstraints:
    return ApprovedConstraints(
        benchmark=analysis.benchmark_requirements[0],
        reasoning_effort=analysis.proposed_reasoning_effort,
        risk=analysis.risk,
        evidence_policy=analysis.evidence_policy,
        cost_quota_preference="preserve_subscription",
    )


def _evaluated_proposal(
    registry: BenchmarkRegistry,
    analysis: AdviserAnalysis,
    candidates: list[CandidateSnapshot],
    *,
    proposal_id: str = "01234567-89ab-cdef-0123-456789abcdef",
) -> RoutingProposal:
    constraints = _constraints(analysis)
    result = evaluate_candidates(registry, analysis, candidates, constraints)
    now = datetime.now(timezone.utc)
    return RoutingProposal(
        proposal_id=proposal_id,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        prompt_fingerprint="sha256:" + "0" * 64,
        workspace_summary="test workspace",
        adviser=analysis.model_copy(update={"disposition": result.disposition}),
        approved_constraints=constraints,
        evaluations=result.evaluations,
        frontier=result.frontier,
        disposition=result.disposition,
    )


def test_adviser_schema_exposes_exact_categorical_vocabularies() -> None:
    schema = AdviserAnalysis.model_json_schema()
    properties = schema["properties"]

    assert properties["difficulty"]["enum"] == ["low", "medium", "high", "frontier"]
    assert properties["risk"]["enum"] == ["low", "medium", "high"]
    assert properties["proposed_reasoning_effort"]["enum"] == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]


@pytest.mark.asyncio
async def test_adviser_repairs_schema_invalid_provider_output_once() -> None:
    valid = _analysis(evidence_policy=EvidencePolicy.PROVISIONAL).model_dump(mode="json")
    invalid = {**valid, "difficulty": "simple"}

    class RepairingClient:
        def __init__(self) -> None:
            self.bodies: list[dict[str, object]] = []

        async def create_response(self, body: dict[str, object]) -> OmniRouteResponse:
            self.bodies.append(body)
            payload = invalid if len(self.bodies) == 1 else valid
            return OmniRouteResponse(
                body={"output_text": json.dumps(payload)},
                headers={},
            )

    client = RepairingClient()
    adviser = OmniRouteRoutingAdviser(cast(OmniRouteClient, client))
    result = await adviser.analyse(
        prompt="Inspect the repository read-only.",
        workspace_summary="test workspace",
        registry=BenchmarkRegistry(slices=[_SLICE], evidence=[], candidates=[]),
        candidates=[_candidate()],
    )

    assert result.difficulty == "medium"
    assert len(client.bodies) == 2
    repair_input = cast(list[dict[str, str]], client.bodies[1]["input"])
    assert "matching this schema exactly" in repair_input[0]["content"]


def test_exact_evidence_uses_conservative_lower_bound() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate, point=0.82, lower=0.61)
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[evidence], candidates=[])
    analysis = _analysis(minimum=0.6)

    result = evaluate_candidates(registry, analysis, [candidate], _constraints(analysis))

    assert result.disposition is Disposition.ROUTE
    assert result.evaluations[0].status is CandidateStatus.PASS
    assert result.evaluations[0].admission_score == 0.61


def test_lower_bound_failure_is_not_rescued_by_point_estimate() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate, point=0.82, lower=0.59)
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[evidence], candidates=[])
    analysis = _analysis(minimum=0.6)

    result = evaluate_candidates(registry, analysis, [candidate], _constraints(analysis))

    evaluation = result.evaluations[0]
    assert evaluation.status is CandidateStatus.EXCLUDED
    assert evaluation.admission_score == 0.59
    assert any("conservative exact score 0.590" in reason for reason in evaluation.exclusions)


def test_missing_evidence_is_unknown_not_zero() -> None:
    candidate = _candidate()
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[], candidates=[])
    analysis = _analysis(evidence_policy=EvidencePolicy.PROVISIONAL)

    result = evaluate_candidates(registry, analysis, [candidate], _constraints(analysis))

    evaluation = result.evaluations[0]
    assert evaluation.evidence_class is EvidenceClass.UNKNOWN
    assert evaluation.admission_score is None
    assert "benchmark evidence is unknown, not zero" in evaluation.caveats
    assert evaluation.status is CandidateStatus.EXCLUDED
    assert result.disposition is Disposition.DECOMPOSE
    assert "unknown benchmark evidence cannot qualify a provisional route" in evaluation.exclusions


def test_provisional_evidence_must_still_meet_the_approved_floor() -> None:
    candidate = _candidate()
    evidence = _evidence(
        candidate,
        evidence_class=EvidenceClass.PROXY,
        point=0.49,
        lower=0.45,
    )
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[evidence], candidates=[])
    analysis = _analysis(minimum=0.5, evidence_policy=EvidencePolicy.PROVISIONAL)

    result = evaluate_candidates(registry, analysis, [candidate], _constraints(analysis))

    assert result.disposition is Disposition.DECOMPOSE
    assert result.evaluations[0].status is CandidateStatus.EXCLUDED
    assert any(
        "provisional evidence score 0.450" in reason for reason in result.evaluations[0].exclusions
    )


@pytest.mark.parametrize(
    ("candidate_updates", "requirements", "effort", "expected"),
    [
        ({"tools": False}, RoutingRequirements(tools=True), "low", "tool-call"),
        (
            {"context_tokens": 8_000},
            RoutingRequirements(minimum_context_tokens=32_000),
            "low",
            "context capacity",
        ),
        ({}, RoutingRequirements(), "high", "reasoning effort"),
        ({"quota_available": False}, RoutingRequirements(), "low", "quota is exhausted"),
    ],
)
def test_structural_incompatibilities_are_hard_exclusions(
    candidate_updates: dict[str, object],
    requirements: RoutingRequirements,
    effort: str,
    expected: str,
) -> None:
    candidate = _candidate(**candidate_updates)
    evidence = _evidence(candidate, effort=effort)
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[evidence], candidates=[])
    analysis = _analysis(effort=effort, requirements=requirements)

    result = evaluate_candidates(registry, analysis, [candidate], _constraints(analysis))

    assert result.evaluations[0].status is CandidateStatus.EXCLUDED
    assert any(expected in reason for reason in result.evaluations[0].exclusions)


def test_adequate_non_codex_candidate_survives_codex_quota_exhaustion() -> None:
    codex = _candidate(quota_available=False, quota_remaining_percent=0)
    free = _candidate(
        "free-low",
        provider_id="opencode",
        model="big-pickle",
        monetary_cost_usd=0,
    )
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(codex), _evidence(free)],
        candidates=[],
    )
    analysis = _analysis()

    result = evaluate_candidates(registry, analysis, [codex, free], _constraints(analysis))

    statuses = {item.candidate.candidate_id: item.status for item in result.evaluations}
    assert statuses == {"codex-low": CandidateStatus.EXCLUDED, "free-low": CandidateStatus.PASS}
    assert result.frontier.passing_exact_candidates == ["free-low"]


def test_no_adequate_candidate_does_not_lower_threshold() -> None:
    candidate = _candidate()
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(candidate, point=0.55, lower=0.45)],
        candidates=[],
    )
    analysis = _analysis(minimum=0.7)

    result = evaluate_candidates(registry, analysis, [candidate], _constraints(analysis))

    assert result.disposition is Disposition.DECOMPOSE
    assert result.frontier.requested_minimum == 0.7
    assert result.frontier.passing_exact_candidates == []


class _FakeOmniRoute:
    def __init__(self, candidates: list[CandidateSnapshot]) -> None:
        self.candidates = candidates
        self.created: list[tuple[str, list[CandidateEvaluation], str]] = []
        self.deleted: list[str] = []

    async def live_candidates(self, _profiles: object) -> list[CandidateSnapshot]:
        return self.candidates

    async def create_derived_combo(
        self,
        proposal_id: str,
        evaluations: list[CandidateEvaluation],
        *,
        reasoning_effort: str,
    ) -> tuple[str, dict[str, object]]:
        self.created.append((proposal_id, evaluations, reasoning_effort))
        name = "custom/o3-route-" + proposal_id.replace("-", "")[:12]
        return name, {"name": name}

    async def delete_derived_combo(self, name: str) -> bool:
        self.deleted.append(name)
        return True


class _FakeAdviser:
    def __init__(
        self,
        initial: AdviserAnalysis,
        decomposed: AdviserAnalysis | None = None,
    ) -> None:
        self.initial = initial
        self.decomposed = decomposed or initial

    async def analyse(self, **_kwargs: object) -> AdviserAnalysis:
        return self.initial

    async def decompose(self, **_kwargs: object) -> AdviserAnalysis:
        return self.decomposed


def _service(
    tmp_path: Path,
    registry: BenchmarkRegistry,
    candidates: list[CandidateSnapshot],
    adviser: AdviserAnalysis,
    *,
    decomposed: AdviserAnalysis | None = None,
) -> tuple[O3RoutingReviewService, _FakeOmniRoute]:
    omni = _FakeOmniRoute(candidates)
    service = O3RoutingReviewService(
        registry=registry,
        omniroute=cast(OmniRouteClient, omni),
        adviser=cast(RoutingAdviser, _FakeAdviser(adviser, decomposed)),
        store=ProposalStore(tmp_path / "o3-state.json"),
    )
    return service, omni


async def test_provisional_candidate_requires_deliberate_acknowledgement(tmp_path: Path) -> None:
    candidate = _candidate()
    evidence = _evidence(candidate, evidence_class=EvidenceClass.PROXY)
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[evidence], candidates=[])
    analysis = _analysis(evidence_policy=EvidencePolicy.PROVISIONAL)
    proposal = _evaluated_proposal(registry, analysis, [candidate])
    service, omni = _service(tmp_path, registry, [candidate], analysis)
    service.store.put(proposal)

    with pytest.raises(RoutingReviewError, match="deliberate acknowledgement"):
        await service.decide_proposal(
            proposal.proposal_id,
            ProposalDecisionRequest(action=DecisionAction.APPROVE),
        )

    approved = await service.decide_proposal(
        proposal.proposal_id,
        ProposalDecisionRequest(
            action=DecisionAction.APPROVE,
            acknowledge_provisional=True,
        ),
    )
    assert approved.derived_combo_name == "custom/o3-route-0123456789ab"
    assert len(omni.created) == 1


async def test_unknown_evidence_requires_confirmed_run_anyway(tmp_path: Path) -> None:
    candidate = _candidate()
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[], candidates=[])
    analysis = _analysis(evidence_policy=EvidencePolicy.PROVISIONAL)
    proposal = _evaluated_proposal(registry, analysis, [candidate])
    service, omni = _service(tmp_path, registry, [candidate], analysis)
    service.store.put(proposal)

    with pytest.raises(RoutingReviewError, match="no structurally usable candidate"):
        await service.decide_proposal(
            proposal.proposal_id,
            ProposalDecisionRequest(action=DecisionAction.APPROVE),
        )

    overridden = await service.decide_proposal(
        proposal.proposal_id,
        ProposalDecisionRequest(
            action=DecisionAction.RUN_ANYWAY,
            confirm_run_anyway=True,
            reason="Live compatibility probe passed",
        ),
    )

    assert overridden.decision is DecisionAction.RUN_ANYWAY
    assert overridden.decision_reason == "Live compatibility probe passed"
    assert overridden.derived_combo_name == "custom/o3-route-0123456789ab"
    assert len(omni.created) == 1


async def test_decomposition_revalidates_subtasks_and_retains_blocked_integration(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(candidate, point=0.7, lower=0.6)],
        candidates=[],
    )
    initial = _analysis(minimum=0.8)
    decomposed = _analysis(
        minimum=0.8,
        decomposition=[
            DecompositionItem(
                objective="Inspect one subsystem independently",
                dependency_order=1,
                benchmark_id=_SLICE.benchmark_id,
                version=_SLICE.version,
                slice_id=_SLICE.slice_id,
                minimum_score=0.5,
                reasoning_effort="low",
                risk="low",
                competence_reduction="The scope no longer requires global integration reasoning.",
            ),
            DecompositionItem(
                objective="Integrate and review the cross-system result",
                dependency_order=2,
                benchmark_id=_SLICE.benchmark_id,
                version=_SLICE.version,
                slice_id=_SLICE.slice_id,
                minimum_score=0.8,
                reasoning_effort="low",
                risk="high",
                competence_reduction="Global integration competence is still required.",
                blocked=True,
            ),
        ],
    )
    service, _ = _service(tmp_path, registry, [candidate], initial, decomposed=decomposed)

    proposal = await service.create_proposal(
        ProposalCreateRequest(prompt="Review and integrate two systems")
    )

    first, integration = proposal.adviser.decomposition
    assert first.passing_candidates == [candidate.candidate_id]
    assert first.blocked is False
    assert integration.passing_candidates == []
    assert integration.blocked is True
    assert proposal.disposition is Disposition.DECOMPOSE


async def test_ttl_cleanup_deletes_abandoned_combo_but_retains_active_session(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    evidence = _evidence(candidate)
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=[evidence], candidates=[])
    analysis = _analysis()
    service, omni = _service(tmp_path, registry, [candidate], analysis)
    now = datetime.now(timezone.utc)
    abandoned = _evaluated_proposal(
        registry,
        analysis,
        [candidate],
        proposal_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ).model_copy(
        update={
            "expires_at": now - timedelta(seconds=1),
            "decision": DecisionAction.APPROVE,
            "derived_combo_name": "custom/o3-route-aaaaaaaaaaaa",
        }
    )
    active = _evaluated_proposal(
        registry,
        analysis,
        [candidate],
        proposal_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ).model_copy(
        update={
            "expires_at": now - timedelta(seconds=1),
            "decision": DecisionAction.APPROVE,
            "derived_combo_name": "custom/o3-route-bbbbbbbbbbbb",
            "session_id": "session-active",
        }
    )
    service.store.put(abandoned)
    service.store.put(active)

    result = await service.cleanup_expired(now=now)

    assert omni.deleted == ["custom/o3-route-aaaaaaaaaaaa"]
    assert result.removed_combos == ["custom/o3-route-aaaaaaaaaaaa"]
    assert result.retained_active == ["custom/o3-route-bbbbbbbbbbbb"]


class _InMemoryComboClient(OmniRouteClient):
    def __init__(self, pool_models: list[dict[str, object]]) -> None:
        super().__init__("http://127.0.0.1:20128", "test-token")
        self.combos: list[dict[str, object]] = [
            {
                "id": "source-pool",
                "name": "custom/o3-codex-pool",
                "strategy": "auto",
                "models": pool_models,
            }
        ]
        self.available_models = {
            str(item["model"]) for item in pool_models if isinstance(item.get("model"), str)
        }

    async def model_ids(self) -> set[str]:
        return set(self.available_models)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> OmniRouteResponse:
        del timeout
        if method == "GET" and path == "/api/combos":
            return OmniRouteResponse(body={"combos": self.combos}, headers={})
        if method == "POST" and path == "/api/combos" and body is not None:
            persisted = json.loads(json.dumps(body))
            for target in persisted.get("models", []):
                provider_id = target.get("providerId")
                model = target.get("model")
                if (
                    isinstance(provider_id, str)
                    and isinstance(model, str)
                    and not model.startswith(f"{provider_id}/")
                ):
                    target["model"] = f"{provider_id}/{model}"
            self.combos.append({"id": f"created-{len(self.combos)}", **persisted})
            return OmniRouteResponse(body={"ok": True}, headers={})
        if method == "DELETE" and path.startswith("/api/combos/"):
            combo_id = path.rsplit("/", 1)[-1]
            self.combos = [item for item in self.combos if item.get("id") != combo_id]
            return OmniRouteResponse(body={"ok": True}, headers={})
        raise AssertionError(f"unexpected request: {method} {path}")


def _selected(candidate: CandidateSnapshot) -> CandidateEvaluation:
    return CandidateEvaluation(
        candidate=candidate,
        status=CandidateStatus.PROVISIONAL,
        evidence_class=EvidenceClass.PROXY,
    )


async def test_live_candidates_accepts_provider_qualified_combo_readback() -> None:
    candidate = _candidate()
    client = _InMemoryComboClient(
        [
            {
                "id": "codex",
                "kind": "model",
                "providerId": "codex",
                "model": "codex/gpt-5.5",
            }
        ]
    )
    profile = CandidateProfile.model_validate(
        candidate.model_dump(include=set(CandidateProfile.model_fields))
    )

    snapshots = await client.live_candidates([profile])

    assert len(snapshots) == 1
    assert snapshots[0].provider_usable is True
    assert snapshots[0].model == "gpt-5.5"


async def test_derived_combo_is_strict_subset_idempotent_and_namespace_safe() -> None:
    codex = _candidate()
    free = _candidate("free-low", provider_id="opencode", model="big-pickle")
    mistral = _candidate("mistral-high", provider_id="mistral", model="small", effort="high")
    pool = [
        {
            "id": "codex",
            "kind": "model",
            "providerId": "codex",
            "model": "codex/gpt-5.5",
        },
        {
            "id": "free",
            "kind": "model",
            "providerId": "opencode",
            "model": "opencode/big-pickle",
        },
        {
            "id": "mistral",
            "kind": "model",
            "providerId": "mistral",
            "model": "mistral/small",
        },
    ]
    client = _InMemoryComboClient(pool)
    proposal_id = "01234567-89ab-cdef-0123-456789abcdef"

    name, first = await client.create_derived_combo(
        proposal_id,
        [_selected(codex), _selected(free)],
        reasoning_effort="low",
    )
    same_name, second = await client.create_derived_combo(
        proposal_id,
        [_selected(codex), _selected(free)],
        reasoning_effort="low",
    )

    assert name == same_name == "custom/o3-route-0123456789ab"
    assert first == second
    assert sum(item.get("name") == name for item in client.combos) == 1

    with pytest.raises(OmniRouteError, match="strict source-pool subset"):
        await client.create_derived_combo(
            "fedcba98-7654-3210-fedc-ba9876543210",
            [_selected(codex), _selected(free), _selected(mistral)],
            reasoning_effort="low",
        )
    with pytest.raises(OmniRouteError, match="outside"):
        await client.delete_derived_combo("custom/best-coding")

    assert await client.delete_derived_combo(name) is True
    assert await client.delete_derived_combo(name) is False


class _CallLogClient(OmniRouteClient):
    def __init__(
        self,
        rows: list[dict[str, object]],
        details: dict[str, dict[str, object]],
    ) -> None:
        super().__init__("http://127.0.0.1:20128", "test-token")
        self.rows = rows
        self.details = details

    async def list_call_logs(self, *, limit: int, offset: int) -> list[dict[str, object]]:
        return self.rows[offset : offset + limit]

    async def get_call_log(self, call_log_id: str) -> dict[str, object]:
        return self.details[call_log_id]


def _call_log_row(
    proposal: RoutingProposal,
    *,
    call_log_id: str = "log-1",
    session_tag: str = "conv-o3",
    combo_name: str | None = None,
    timestamp: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": call_log_id,
        "timestamp": (timestamp or proposal.created_at + timedelta(seconds=1)).isoformat(),
        "path": "/v1/responses",
        "method": "POST",
        "comboName": combo_name or proposal.derived_combo_name,
        "requestedModel": proposal.derived_combo_name,
        "provider": "codex",
        "model": "gpt-5.5",
        "connectionId": "connection-safe-id",
        "correlationId": "correlation-safe-id",
        "sessionTag": session_tag,
        "status": 200,
        "duration": 1234,
        "tokens": {
            "in": 21,
            "out": 8,
            "reasoning": 3,
            "cacheRead": 5,
            "cacheWrite": None,
        },
        "requestSummary": "must not be persisted",
    }


async def test_call_log_matching_persists_only_sanitized_responses_metadata() -> None:
    candidate = _candidate()
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(candidate)],
        candidates=[],
    )
    proposal = _evaluated_proposal(registry, _analysis(), [candidate]).model_copy(
        update={"derived_combo_name": "custom/o3-route-0123456789ab"}
    )
    matching = _call_log_row(proposal)
    rows = [
        matching,
        _call_log_row(
            proposal,
            call_log_id="second-codex-conversation",
            session_tag="conv-other",
        ),
        _call_log_row(
            proposal,
            call_log_id="wrong-combo",
            combo_name="custom/o3-route-aaaaaaaaaaaa",
        )
        | {"requestedModel": "custom/o3-route-aaaaaaaaaaaa"},
        _call_log_row(
            proposal,
            call_log_id="too-old",
            timestamp=proposal.created_at - timedelta(minutes=6),
        ),
    ]
    client = _CallLogClient(
        rows,
        {
            "log-1": {
                **matching,
                "requestBody": {
                    "model": proposal.derived_combo_name,
                    "reasoning": {"effort": "low"},
                    "input": "sensitive task body must never enter audit data",
                },
                "responseBody": {"output": "sensitive model body"},
            },
            "second-codex-conversation": {},
        },
    )

    records = await client.execution_provenance_for_session(
        derived_combo_name="custom/o3-route-0123456789ab",
        proposal_created_at=proposal.created_at,
    )

    assert len(records) == 2
    assert {record.session_tag for record in records} == {"conv-o3", "conv-other"}
    record = next(record for record in records if record.call_log_id == "log-1")
    assert record.call_log_id == "log-1"
    assert record.reasoning_effort == "low"
    assert record.token_usage.reasoning_tokens == 3
    serialized = record.model_dump_json()
    assert "sensitive task body" not in serialized
    assert "sensitive model body" not in serialized
    assert "requestSummary" not in serialized


async def test_terminal_status_syncs_execution_provenance_and_manual_outcome(
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(candidate)],
        candidates=[],
    )
    analysis = _analysis()
    proposal = _evaluated_proposal(registry, analysis, [candidate]).model_copy(
        update={
            "decision": DecisionAction.APPROVE,
            "derived_combo_name": "custom/o3-route-0123456789ab",
            "session_id": "conv-o3",
        }
    )
    matching = _call_log_row(proposal)
    client = _CallLogClient(
        [matching],
        {"log-1": {**matching, "requestBody": {"reasoning": {"effort": "low"}}}},
    )
    service = O3RoutingReviewService(
        registry=registry,
        omniroute=client,
        adviser=cast(RoutingAdviser, _FakeAdviser(analysis)),
        store=ProposalStore(tmp_path / "o3-state.json"),
    )
    service.store.put(proposal)

    [synced] = await service.sync_execution_provenance(
        "conv-o3",
        external_status="idle",
    )
    [synced_again] = await service.sync_execution_provenance(
        "conv-o3",
        external_status="idle",
    )

    assert synced.actual_provider == "codex"
    assert synced.actual_model == "gpt-5.5"
    assert synced.actual_reasoning_effort == "low"
    assert synced.execution_status == "idle"
    assert synced.task_outcome == "completed"
    assert synced.terminal_disposition == "completed"
    assert [item.call_log_id for item in synced_again.execution_provenance] == ["log-1"]

    manual = service.record_outcome(
        proposal.proposal_id,
        ProposalOutcomeRequest(
            outcome="verified by the operator",
            terminal_disposition="accepted",
        ),
    )
    assert manual.task_outcome == "verified by the operator"
    assert manual.actual_provider == "codex"
    assert manual.actual_model == "gpt-5.5"


async def test_failed_status_records_outcome_when_no_call_log_exists(tmp_path: Path) -> None:
    candidate = _candidate()
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(candidate)],
        candidates=[],
    )
    analysis = _analysis()
    proposal = _evaluated_proposal(registry, analysis, [candidate]).model_copy(
        update={
            "decision": DecisionAction.APPROVE,
            "derived_combo_name": "custom/o3-route-0123456789ab",
            "session_id": "conv-o3",
        }
    )
    service = O3RoutingReviewService(
        registry=registry,
        omniroute=_CallLogClient([], {}),
        adviser=cast(RoutingAdviser, _FakeAdviser(analysis)),
        store=ProposalStore(tmp_path / "o3-state.json"),
    )
    service.store.put(proposal)

    [failed] = await service.sync_execution_provenance(
        "conv-o3",
        external_status="failed",
    )

    assert failed.execution_provenance == []
    assert failed.execution_status == "failed"
    assert failed.task_outcome == "failed"
    assert failed.terminal_disposition == "failed"


class _RouteService:
    def __init__(self, proposal: RoutingProposal, registry: BenchmarkRegistry) -> None:
        self.proposal = proposal
        self.registry = registry
        self.create_calls = 0

    async def create_proposal(self, _body: ProposalCreateRequest) -> RoutingProposal:
        self.create_calls += 1
        return self.proposal


async def test_mutating_routes_require_auth_json_and_trusted_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    candidate = _candidate()
    registry = BenchmarkRegistry(
        slices=[_SLICE],
        evidence=[_evidence(candidate)],
        candidates=[],
    )
    proposal = _evaluated_proposal(registry, _analysis(), [candidate])
    route_service = _RouteService(proposal, registry)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_omnigent_error(
        _request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    app.include_router(
        create_o3_routing_review_router(
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            service_factory=lambda: cast(O3RoutingReviewService, route_service),
        ),
        prefix="/v1",
    )
    transport = httpx.ASGITransport(app=app)
    url = "/v1/o3/routing-review/proposals"
    payload = {"prompt": "Inspect this repository"}
    auth = {"X-Forwarded-Email": "alice@example.com"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated = await client.post(url, json=payload)
        wrong_type = await client.post(
            url,
            content='{"prompt":"Inspect this repository"}',
            headers={**auth, "Content-Type": "text/plain"},
        )
        untrusted = await client.post(
            url,
            json=payload,
            headers={**auth, "Origin": "https://attacker.example"},
        )
        accepted = await client.post(
            url,
            json=payload,
            headers={**auth, "Origin": "http://127.0.0.1:5173"},
        )

    assert unauthenticated.status_code == 401
    assert wrong_type.status_code == 415
    assert untrusted.status_code == 403
    assert accepted.status_code == 201
    assert accepted.json()["proposal_id"] == proposal.proposal_id
    assert route_service.create_calls == 1
