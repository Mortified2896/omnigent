"""Typed contracts for the O3 routing-review API and audit store."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

Difficulty: TypeAlias = Literal["low", "medium", "high", "frontier"]
Risk: TypeAlias = Literal["low", "medium", "high"]
ReasoningEffort: TypeAlias = Literal["low", "medium", "high", "xhigh"]
CostQuotaPreference: TypeAlias = Literal[
    "balanced", "preserve_subscription", "lowest_cost", "lowest_latency"
]


class StrictModel(BaseModel):
    """Base model for persisted and wire contracts."""

    model_config = ConfigDict(extra="forbid")


class EvidenceClass(StrEnum):
    EXACT = "exact"
    PROXY = "proxy"
    ADVISORY = "advisory"
    UNKNOWN = "unknown"


class EvidencePolicy(StrEnum):
    STRICT = "strict"
    PROVISIONAL = "provisional"


class Disposition(StrEnum):
    ROUTE = "route"
    BORDERLINE = "borderline"
    DECOMPOSE = "decompose"
    DEFER = "defer"


class DecisionAction(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    DEFER = "defer"
    RUN_ANYWAY = "run_anyway"


class CandidateStatus(StrEnum):
    PASS = "pass"
    PROVISIONAL = "provisional"
    EXCLUDED = "excluded"


class BenchmarkSlice(StrictModel):
    benchmark_id: str
    version: str
    slice_id: str
    label: str
    interpretation: str
    task_ids: list[str]
    task_manifest_digest: str
    official: bool = False


class BenchmarkRequirement(StrictModel):
    benchmark_id: str
    version: str
    slice_id: str
    minimum_score: float = Field(ge=0, le=1)
    reason: str


class BenchmarkEvidence(StrictModel):
    benchmark_id: str
    benchmark_version: str
    slice_id: str
    task_manifest_digest: str
    harness: str
    harness_version: str | None = None
    model: str
    provider_path: str | None = None
    reasoning_effort: str
    point_score: float = Field(ge=0, le=1)
    confidence_lower: float | None = Field(default=None, ge=0, le=1)
    confidence_upper: float | None = Field(default=None, ge=0, le=1)
    number_of_tasks: int = Field(ge=1)
    number_of_attempts: int = Field(ge=1)
    evidence_class: EvidenceClass
    source_type: str
    source_reference: str
    evaluation_date: str

    @property
    def admission_score(self) -> float:
        """Return the conservative score used by the adequacy gate."""
        return self.confidence_lower if self.confidence_lower is not None else self.point_score


class RoutingRequirements(StrictModel):
    terminal: bool = True
    tools: bool = True
    minimum_context_tokens: int = Field(default=0, ge=0)
    vision: bool = False


class DecompositionItem(StrictModel):
    objective: str
    dependency_order: int = Field(ge=1)
    benchmark_id: str
    version: str
    slice_id: str
    minimum_score: float = Field(ge=0, le=1)
    reasoning_effort: ReasoningEffort
    risk: Risk
    passing_candidates: list[str] = Field(default_factory=list)
    competence_reduction: str
    blocked: bool = False


class AdviserAnalysis(StrictModel):
    """Model-authored requirements only; provider/model selection is absent by design."""

    task_summary: str
    task_classification: str
    difficulty: Difficulty
    risk: Risk
    requirements: RoutingRequirements
    benchmark_requirements: list[BenchmarkRequirement] = Field(min_length=1, max_length=1)
    proposed_reasoning_effort: ReasoningEffort
    evidence_policy: EvidencePolicy
    disposition: Disposition
    confidence: float = Field(ge=0, le=1)
    rationale: str
    decomposition: list[DecompositionItem] = Field(default_factory=list)


class CandidateProfile(StrictModel):
    candidate_id: str
    provider_id: str
    model: str
    catalogue_model_id: str
    harness: str = "codex-native"
    supported_reasoning_efforts: list[str]
    context_tokens: int | None = None
    terminal: bool = True
    tools: bool = True
    vision: bool = False
    responses_api: bool = True
    monetary_cost_usd: float | None = Field(default=None, ge=0)
    cost_source: str
    quota_source: str
    last_full_probe_at: str
    probe_reference: str


class CandidateSnapshot(CandidateProfile):
    provider_usable: bool
    model_present: bool
    quota_available: bool | None = None
    quota_remaining_percent: float | None = Field(default=None, ge=0, le=100)
    quota_reset_at: str | None = None
    recent_success_rate: float | None = Field(default=None, ge=0, le=1)
    recent_retry_rate: float | None = Field(default=None, ge=0, le=1)
    latency_ms: float | None = Field(default=None, ge=0)


class RankingInputs(StrictModel):
    evidence_confidence: float = Field(ge=0, le=1)
    competence_margin: float | None = None
    health: float = Field(ge=0, le=1)
    estimated_monetary_cost_usd: float | None = None
    quota_remaining_percent: float | None = None
    quota_reset_at: str | None = None
    quota_scarcity_penalty: float = Field(ge=0, le=1)
    recent_failure_rate: float | None = Field(default=None, ge=0, le=1)
    recent_retry_rate: float | None = Field(default=None, ge=0, le=1)
    latency_ms: float | None = None
    deterministic_score: float


class CandidateEvaluation(StrictModel):
    candidate: CandidateSnapshot
    status: CandidateStatus
    evidence_class: EvidenceClass
    admission_score: float | None = None
    evidence: list[BenchmarkEvidence] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    ranking: RankingInputs | None = None


class FrontierSnapshot(StrictModel):
    requested_minimum: float
    global_measured_frontier: float | None
    accessible_configured_frontier: float | None
    healthy_available_frontier: float | None
    passing_exact_candidates: list[str] = Field(default_factory=list)
    provisional_candidates: list[str] = Field(default_factory=list)
    capability_gap: str | None = None


class ApprovedConstraints(StrictModel):
    benchmark: BenchmarkRequirement
    reasoning_effort: ReasoningEffort
    risk: Risk
    evidence_policy: EvidencePolicy
    cost_quota_preference: CostQuotaPreference = "balanced"


class ExecutionTokenUsage(StrictModel):
    """Sanitized token counters copied from an OmniRoute call-log row."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)


class ExecutionProvenance(StrictModel):
    """Account-safe execution metadata; request and response bodies are excluded."""

    call_log_id: str
    timestamp: datetime
    path: str
    method: str
    session_tag: str
    combo_name: str | None = None
    requested_model: str | None = None
    provider: str
    model: str
    connection_id: str | None = None
    correlation_id: str | None = None
    http_status: int
    duration_ms: float | None = Field(default=None, ge=0)
    token_usage: ExecutionTokenUsage
    reasoning_effort: str | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class RoutingProposal(StrictModel):
    schema_version: int = 1
    proposal_id: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    prompt_fingerprint: str
    workspace_summary: str
    adviser: AdviserAnalysis
    approved_constraints: ApprovedConstraints
    evaluations: list[CandidateEvaluation]
    frontier: FrontierSnapshot
    disposition: Disposition
    decision: DecisionAction | None = None
    decision_reason: str | None = None
    derived_combo_name: str | None = None
    derived_combo_definition: dict[str, object] | None = None
    session_id: str | None = None
    actual_provider: str | None = None
    actual_model: str | None = None
    actual_reasoning_effort: str | None = None
    execution_provenance: list[ExecutionProvenance] = Field(default_factory=list)
    execution_status: str | None = None
    provenance_synced_at: datetime | None = None
    task_outcome: str | None = None
    terminal_disposition: str | None = None


class ProposalCreateRequest(StrictModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    workspace_summary: str = Field(default="", max_length=20_000)


class ProposalAdjustmentRequest(StrictModel):
    benchmark_id: str | None = None
    version: str | None = None
    slice_id: str | None = None
    minimum_score: float | None = Field(default=None, ge=0, le=1)
    reasoning_effort: ReasoningEffort | None = None
    risk: Risk | None = None
    evidence_policy: EvidencePolicy | None = None
    cost_quota_preference: CostQuotaPreference | None = None


class ProposalDecisionRequest(StrictModel):
    action: DecisionAction
    acknowledge_provisional: bool = False
    confirm_run_anyway: bool = False
    reason: str | None = Field(default=None, max_length=2_000)


class ProposalSessionLinkRequest(StrictModel):
    session_id: str = Field(min_length=1, max_length=256)


class ProposalOutcomeRequest(StrictModel):
    outcome: str = Field(min_length=1, max_length=2_000)
    terminal_disposition: str = Field(min_length=1, max_length=100)
    actual_provider: str | None = Field(default=None, max_length=200)
    actual_model: str | None = Field(default=None, max_length=300)
    actual_reasoning_effort: str | None = Field(default=None, max_length=30)


class CleanupResult(StrictModel):
    inspected: int
    removed_combos: list[str] = Field(default_factory=list)
    retained_active: list[str] = Field(default_factory=list)
    failures: dict[str, str] = Field(default_factory=dict)
