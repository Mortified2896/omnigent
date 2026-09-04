"""Cross-harness public-evidence policy for the O3 routing review."""

from __future__ import annotations

import json

from omnigent.server.o3_routing_review.adviser import _json_payload
from omnigent.server.o3_routing_review.evaluator import evaluate_candidates
from omnigent.server.o3_routing_review.models import (
    AdviserAnalysis,
    ApprovedConstraints,
    BenchmarkEvidence,
    BenchmarkRequirement,
    CandidateSnapshot,
    CandidateStatus,
    Disposition,
    EvidenceClass,
    EvidencePolicy,
    RoutingRequirements,
)
from omnigent.server.o3_routing_review.registry import BenchmarkRegistry, default_slices

_SLICE = next(item for item in default_slices() if item.slice_id == "tb4.overall")


def _candidate(
    model: str, *, provider: str = "openrouter", effort: str = "low"
) -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id=f"{provider}-{model}-{effort}",
        provider_id=provider,
        model=model,
        catalogue_model_id=f"{provider}/{model}",
        supported_reasoning_efforts=[effort],
        context_tokens=128_000,
        terminal=True,
        tools=True,
        vision=False,
        responses_api=True,
        monetary_cost_usd=0,
        cost_source="test",
        quota_source="test",
        last_full_probe_at="2026-09-03T00:00:00+08:00",
        probe_reference="test compatibility probe",
        provider_usable=True,
        model_present=True,
        quota_available=True,
        quota_remaining_percent=100,
        quota_reset_at=None,
        recent_success_rate=1,
        recent_retry_rate=0,
        latency_ms=100,
    )


def _evidence(
    model: str,
    *,
    harness: str,
    lower: float,
    point: float | None = None,
    provider_path: str = "publisher",
    effort: str = "max",
) -> BenchmarkEvidence:
    return BenchmarkEvidence(
        benchmark_id="terminal-bench",
        benchmark_version="4.0.0",
        slice_id=_SLICE.slice_id,
        task_manifest_digest=_SLICE.task_manifest_digest,
        harness=harness,
        harness_version="published",
        model=model,
        provider_path=provider_path,
        reasoning_effort=effort,
        point_score=point if point is not None else lower,
        confidence_lower=lower,
        confidence_upper=min(1, lower + 0.05),
        number_of_tasks=66,
        number_of_attempts=330,
        evidence_class=EvidenceClass.EXACT,
        source_type="official_leaderboard_submission",
        source_reference=f"https://example.test/{harness}/{model}",
        evaluation_date="2026-09-01",
    )


def _analysis(*, minimum: float = 0.2, effort: str = "low") -> AdviserAnalysis:
    requirement = BenchmarkRequirement(
        benchmark_id="terminal-bench",
        version="4.0.0",
        slice_id=_SLICE.slice_id,
        minimum_score=minimum,
        reason="terminal execution competence",
    )
    return AdviserAnalysis(
        task_summary="Inspect a repository",
        task_classification="systems",
        difficulty="medium",
        risk="low",
        requirements=RoutingRequirements(),
        benchmark_requirements=[requirement],
        proposed_reasoning_effort=effort,
        evidence_policy=EvidencePolicy.PROVISIONAL,
        disposition=Disposition.BORDERLINE,
        confidence=0.8,
        rationale="A terminal-capable model is required.",
    )


def _evaluate(candidate: CandidateSnapshot, evidence: list[BenchmarkEvidence]):
    analysis = _analysis(effort=candidate.supported_reasoning_efforts[0])
    constraints = ApprovedConstraints(
        benchmark=analysis.benchmark_requirements[0],
        reasoning_effort=analysis.proposed_reasoning_effort,
        risk=analysis.risk,
        evidence_policy=analysis.evidence_policy,
        cost_quota_preference="balanced",
    )
    registry = BenchmarkRegistry(slices=[_SLICE], evidence=evidence, candidates=[])
    return evaluate_candidates(registry, analysis, [candidate], constraints).evaluations[0]


def test_cross_harness_results_estimate_codex_with_unmodified_median() -> None:
    candidate = _candidate("claude-opus-5")
    evaluation = _evaluate(
        candidate,
        [
            _evidence("anthropic/claude-opus-5", harness="claude-code", lower=0.48),
            _evidence("anthropic/claude-opus-5", harness="mini-swe-agent", lower=0.38),
        ],
    )

    assert evaluation.status is CandidateStatus.PROVISIONAL
    assert evaluation.evidence_class is EvidenceClass.PROXY
    assert evaluation.admission_score == 0.43
    assert any("identity-transfer estimate" in caveat for caveat in evaluation.caveats)
    assert any("no harness penalty or bonus" in caveat for caveat in evaluation.caveats)


def test_published_codex_result_takes_priority_over_other_harnesses() -> None:
    candidate = _candidate("gpt-5.6-sol", provider="codex")
    evaluation = _evaluate(
        candidate,
        [
            _evidence("openai/gpt-5.6-sol", harness="codex", lower=0.33),
            _evidence("openai/gpt-5.6-sol", harness="claude-code", lower=0.80),
        ],
    )

    assert evaluation.status is CandidateStatus.PROVISIONAL
    assert evaluation.admission_score == 0.33
    assert any("same-model Codex-harness" in caveat for caveat in evaluation.caveats)


def test_provider_prefix_is_not_part_of_model_identity() -> None:
    candidate = _candidate("gpt-5.6-sol", provider="codex")
    evaluation = _evaluate(
        candidate,
        [
            _evidence("openai/gpt-5.6-sol", harness="codex", lower=0.33),
            _evidence("openai/gpt-5.6-terra", harness="codex", lower=0.99),
        ],
    )

    assert evaluation.admission_score == 0.33
    assert [item.model for item in evaluation.evidence] == ["openai/gpt-5.6-sol"]


def test_unknown_model_remains_unknown_instead_of_cross_model_estimation() -> None:
    candidate = _candidate("mistral-small-latest", provider="mistral", effort="high")
    evaluation = _evaluate(
        candidate,
        [_evidence("openai/gpt-5.6-sol", harness="codex", lower=0.33)],
    )

    assert evaluation.status is CandidateStatus.EXCLUDED
    assert evaluation.evidence_class is EvidenceClass.UNKNOWN
    assert evaluation.admission_score is None
    assert evaluation.evidence == []


def test_adviser_payload_excludes_candidates_scores_and_provenance() -> None:
    candidate = _candidate("claude-opus-5")
    registry = BenchmarkRegistry(
        slices=default_slices(),
        evidence=[_evidence("anthropic/claude-opus-5", harness="claude-code", lower=0.48)],
        candidates=[],
    )

    payload = json.loads(
        _json_payload(
            prompt="Inspect the repository",
            workspace_summary="Workspace: /tmp/repo",
            registry=registry,
            candidates=[candidate],
        )
    )
    serialized = json.dumps(payload)

    assert set(payload) == {
        "unchanged_user_task",
        "workspace_summary",
        "allowed_benchmark_slices",
        "decomposition_context",
    }
    assert [item["slice_id"] for item in payload["allowed_benchmark_slices"]] == [
        "tb4.overall"
    ]
    assert "claude-opus-5" not in serialized
    assert "claude-code" not in serialized
    assert "point_score" not in serialized
    assert "provider_usable" not in serialized
    assert "monetary_cost_usd" not in serialized
