"""Deterministic adequacy gate, frontier computation, and efficiency ranking."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .models import (
    AdviserAnalysis,
    ApprovedConstraints,
    BenchmarkEvidence,
    CandidateEvaluation,
    CandidateSnapshot,
    CandidateStatus,
    Disposition,
    EvidenceClass,
    EvidencePolicy,
    FrontierSnapshot,
    RankingInputs,
)
from .registry import BenchmarkRegistry, is_codex_harness

_RISK_MARGIN = {"low": 0.0, "medium": 0.02, "high": 0.05}
_CROSS_HARNESS_ADMISSION_MARGIN = 0.03


@dataclass(frozen=True)
class EvaluationResult:
    evaluations: list[CandidateEvaluation]
    frontier: FrontierSnapshot
    disposition: Disposition


@dataclass(frozen=True)
class _EvidenceAggregate:
    evidence_class: EvidenceClass
    admission_score: float | None
    exact: bool
    caveats: list[str]


def _aggregate_evidence(evidence: list[BenchmarkEvidence]) -> _EvidenceAggregate:
    """Build one expected Codex score without exposing estimates to the adviser."""
    exact = [item for item in evidence if item.evidence_class is EvidenceClass.EXACT]
    if exact:
        return _EvidenceAggregate(
            evidence_class=EvidenceClass.EXACT,
            admission_score=median(item.admission_score for item in exact),
            exact=True,
            caveats=[],
        )

    usable = [
        item
        for item in evidence
        if item.evidence_class in {EvidenceClass.PROXY, EvidenceClass.ADVISORY}
    ]
    direct_codex = [item for item in usable if is_codex_harness(item.harness)]
    if direct_codex:
        count = len(direct_codex)
        return _EvidenceAggregate(
            evidence_class=(
                EvidenceClass.PROXY
                if any(item.evidence_class is EvidenceClass.PROXY for item in direct_codex)
                else EvidenceClass.ADVISORY
            ),
            admission_score=median(item.admission_score for item in direct_codex),
            exact=False,
            caveats=[
                "expected Codex score uses the median of "
                f"{count} published same-model Codex-harness result(s); differing execution "
                "configuration keeps the estimate provisional"
            ],
        )

    if usable:
        harnesses = ", ".join(sorted({item.harness for item in usable}))
        count = len(usable)
        source_score = median(item.admission_score for item in usable)
        admission_score = round(
            max(0.0, source_score - _CROSS_HARNESS_ADMISSION_MARGIN), 12
        )
        return _EvidenceAggregate(
            evidence_class=(
                EvidenceClass.PROXY
                if any(item.evidence_class is EvidenceClass.PROXY for item in usable)
                else EvidenceClass.ADVISORY
            ),
            admission_score=admission_score,
            exact=False,
            caveats=[
                "expected Codex score uses an explicit cross-harness routing-policy buffer: "
                f"median conservative same-model score {source_score:.3f} from {count} "
                f"published result(s) ({harnesses}) minus "
                f"{_CROSS_HARNESS_ADMISSION_MARGIN:.3f} absolute = "
                f"{admission_score:.3f}; the buffer is a policy assumption, not measured "
                "degradation"
            ],
        )

    return _EvidenceAggregate(
        evidence_class=EvidenceClass.UNKNOWN,
        admission_score=None,
        exact=False,
        caveats=["benchmark evidence is unknown, not zero"],
    )


def _quota_scarcity(candidate: CandidateSnapshot) -> float:
    remaining = candidate.quota_remaining_percent
    if remaining is not None:
        if remaining <= 0:
            return 1.0
        if remaining < 10:
            return 0.9
        if remaining < 25:
            return 0.7
        if remaining < 50:
            return 0.45
        return 0.15
    # Unknown subscription headroom is deliberately not treated as free.
    return 0.55 if candidate.provider_id == "codex" else 0.25


def _ranking(
    candidate: CandidateSnapshot,
    *,
    evidence_class: EvidenceClass,
    admission_score: float | None,
    minimum: float,
    preference: str,
) -> RankingInputs:
    margin = admission_score - minimum if admission_score is not None else None
    evidence_confidence = {
        EvidenceClass.EXACT: 0.9,
        EvidenceClass.PROXY: 0.55,
        EvidenceClass.ADVISORY: 0.35,
        EvidenceClass.UNKNOWN: 0.15,
    }[evidence_class]
    health = candidate.recent_success_rate if candidate.recent_success_rate is not None else 0.75
    failure_rate = 1 - health
    retry_rate = candidate.recent_retry_rate
    scarcity = _quota_scarcity(candidate)
    if candidate.monetary_cost_usd is None:
        cost_value = 0.5
    else:
        cost_value = 1 / (1 + candidate.monetary_cost_usd * 100)
    latency_value = 0.5 if candidate.latency_ms is None else 1 / (1 + candidate.latency_ms / 1_000)
    normalized_margin = 0.0 if margin is None else max(-1.0, min(1.0, margin * 5))
    score = (
        0.34 * evidence_confidence
        + 0.18 * normalized_margin
        + 0.18 * health
        + 0.10 * (1 - scarcity)
        + 0.05 * cost_value
        + 0.05 * latency_value
    )
    if preference == "preserve_subscription" and candidate.provider_id == "codex":
        score -= 0.15 * scarcity
    elif preference == "lowest_cost":
        score += 0.10 * cost_value
    elif preference == "lowest_latency":
        score += 0.10 * latency_value
    return RankingInputs(
        evidence_confidence=evidence_confidence,
        competence_margin=margin,
        health=health,
        estimated_monetary_cost_usd=candidate.monetary_cost_usd,
        quota_remaining_percent=candidate.quota_remaining_percent,
        quota_reset_at=candidate.quota_reset_at,
        quota_scarcity_penalty=scarcity,
        recent_failure_rate=failure_rate,
        recent_retry_rate=retry_rate,
        latency_ms=candidate.latency_ms,
        deterministic_score=round(score, 6),
    )


def _hard_exclusions(candidate: CandidateSnapshot, analysis: AdviserAnalysis) -> list[str]:
    requirements = analysis.requirements
    exclusions: list[str] = []
    if not candidate.provider_usable:
        exclusions.append("provider connection is not currently usable")
    if not candidate.model_present:
        exclusions.append("model is absent from the live OmniRoute catalogue")
    if not candidate.responses_api:
        exclusions.append("Codex Responses API compatibility is not qualified")
    if requirements.terminal and not candidate.terminal:
        exclusions.append("terminal execution is required")
    if requirements.tools and not candidate.tools:
        exclusions.append("a complete tool-call round trip is required")
    if requirements.vision and not candidate.vision:
        exclusions.append("vision is required")
    if requirements.minimum_context_tokens > 0:
        if candidate.context_tokens is None:
            exclusions.append("context capacity is unknown")
        elif candidate.context_tokens < requirements.minimum_context_tokens:
            exclusions.append(
                f"context capacity {candidate.context_tokens} is below "
                f"{requirements.minimum_context_tokens}"
            )
    if analysis.proposed_reasoning_effort not in candidate.supported_reasoning_efforts:
        exclusions.append(
            f"reasoning effort {analysis.proposed_reasoning_effort!r} was not fully qualified"
        )
    if candidate.quota_available is False or candidate.quota_remaining_percent == 0:
        exclusions.append("provider quota is exhausted")
    return exclusions


def evaluate_candidates(
    registry: BenchmarkRegistry,
    analysis: AdviserAnalysis,
    candidates: list[CandidateSnapshot],
    constraints: ApprovedConstraints,
) -> EvaluationResult:
    """Evaluate every source-pool candidate without delegating selection to the LLM."""
    requirement = constraints.benchmark
    registry.require_slice(requirement)
    risk_margin = _RISK_MARGIN[constraints.risk]
    required_score = min(1.0, requirement.minimum_score + risk_margin)
    evaluations: list[CandidateEvaluation] = []

    for candidate in candidates:
        exclusions = _hard_exclusions(
            candidate,
            analysis.model_copy(
                update={
                    "proposed_reasoning_effort": constraints.reasoning_effort,
                    "risk": constraints.risk,
                    "evidence_policy": constraints.evidence_policy,
                }
            ),
        )
        evidence = registry.evidence_for(requirement, candidate, constraints.reasoning_effort)
        aggregate = _aggregate_evidence(evidence)
        caveats = list(aggregate.caveats)
        evidence_class = aggregate.evidence_class
        admission_score = aggregate.admission_score

        if admission_score is None:
            exclusions.append("unknown benchmark evidence cannot qualify a provisional route")
        elif admission_score < required_score:
            prefix = "conservative exact" if aggregate.exact else "provisional evidence"
            exclusions.append(
                f"{prefix} score {admission_score:.3f} is below the "
                f"risk-adjusted floor {required_score:.3f}"
            )

        if not aggregate.exact and constraints.evidence_policy is EvidencePolicy.STRICT:
            exclusions.append("strict evidence policy requires an exact benchmark record")

        if exclusions:
            status = CandidateStatus.EXCLUDED
            ranking = None
        else:
            status = CandidateStatus.PASS if aggregate.exact else CandidateStatus.PROVISIONAL
            ranking = _ranking(
                candidate,
                evidence_class=evidence_class,
                admission_score=admission_score,
                minimum=required_score,
                preference=constraints.cost_quota_preference,
            )
        evaluations.append(
            CandidateEvaluation(
                candidate=candidate,
                status=status,
                evidence_class=evidence_class,
                admission_score=admission_score,
                evidence=evidence,
                exclusions=exclusions,
                caveats=caveats,
                ranking=ranking,
            )
        )

    evaluations.sort(
        key=lambda item: (
            item.status is CandidateStatus.PASS,
            item.status is CandidateStatus.PROVISIONAL,
            item.ranking.deterministic_score if item.ranking is not None else float("-inf"),
            item.candidate.candidate_id,
        ),
        reverse=True,
    )
    exact_ids = [
        item.candidate.candidate_id for item in evaluations if item.status is CandidateStatus.PASS
    ]
    provisional_ids = [
        item.candidate.candidate_id
        for item in evaluations
        if item.status is CandidateStatus.PROVISIONAL
    ]
    if exact_ids:
        disposition = Disposition.ROUTE
    elif provisional_ids:
        disposition = Disposition.BORDERLINE
    else:
        disposition = Disposition.DECOMPOSE

    configured_exact = [
        item.admission_score
        for item in evaluations
        if item.evidence_class is EvidenceClass.EXACT and item.admission_score is not None
    ]
    healthy_exact = [
        item.admission_score
        for item in evaluations
        if item.status is CandidateStatus.PASS and item.admission_score is not None
    ]
    if exact_ids:
        gap = None
    elif provisional_ids:
        gap = "No currently accessible configuration has exact evidence at the approved floor."
    else:
        gap = "No source-pool configuration satisfies all approved hard constraints."
    frontier = FrontierSnapshot(
        requested_minimum=requirement.minimum_score,
        global_measured_frontier=registry.global_frontier(requirement),
        accessible_configured_frontier=max(configured_exact) if configured_exact else None,
        healthy_available_frontier=max(healthy_exact) if healthy_exact else None,
        passing_exact_candidates=exact_ids,
        provisional_candidates=provisional_ids,
        capability_gap=gap,
    )
    return EvaluationResult(evaluations=evaluations, frontier=frontier, disposition=disposition)


def hard_capable_candidates(evaluations: list[CandidateEvaluation]) -> list[CandidateEvaluation]:
    """Candidates usable for an explicitly confirmed benchmark override.

    Benchmark-only exclusions are removed, while compatibility, quota, context,
    tool, and effort failures remain structural.
    """
    benchmark_markers = (
        "strict evidence policy",
        "conservative exact score",
        "provisional evidence score",
        "unknown benchmark evidence",
    )
    capable: list[CandidateEvaluation] = []
    for item in evaluations:
        structural = [
            reason for reason in item.exclusions if not reason.startswith(benchmark_markers)
        ]
        if not structural:
            capable.append(item)
    return capable
