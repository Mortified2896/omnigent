"""Typed contracts for the O3 routing-review API and audit store."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, computed_field

Difficulty: TypeAlias = Literal["easy", "normal", "moderate", "hard", "frontier"]
Risk: TypeAlias = Literal["low", "medium", "high"]
ReasoningEffort: TypeAlias = Literal["low", "medium", "high", "xhigh"]
ResourceAdviceAction: TypeAlias = Literal["start_now", "wait", "ask_to_lower_floor"]
CostQuotaPreference: TypeAlias = Literal[
    "balanced", "preserve_subscription", "lowest_cost", "lowest_latency"
]


class StrictModel(BaseModel):
    """Base model for persisted and wire contracts."""

    model_config = ConfigDict(extra="forbid")


class ExecutionMode(StrEnum):
    TOOL_CAPABLE_NATIVE = "tool_capable_native"
    HARD_TOOL_FREE = "hard_tool_free"


class ExecutionOption(StrictModel):
    mode: ExecutionMode
    route: str
    provider: str
    cost_class: str
    capability_score_lower: float
    reason: str


class EvidenceClass(StrEnum):
    EXACT = "exact"
    PROXY = "proxy"
    ADVISORY = "advisory"
    FORECAST = "forecast"
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
    WAIT = "wait"
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


class BenchmarkSelection(StrictModel):
    benchmark_id: str
    version: str
    slice_id: str
    reason: str


class BenchmarkRequirement(BenchmarkSelection):
    minimum_score: float = Field(ge=0, le=1)
    difficulty: Difficulty | None = None
    calibration_version: str | None = None


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
    plausible_lower: float | None = Field(default=None, ge=0, le=1)
    plausible_upper: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    forecaster_version: str | None = None
    created_at: str | None = None
    rationale: str | None = None

    @property
    def admission_score(self) -> float:
        """Return the conservative score used by the adequacy gate."""
        return self.confidence_lower if self.confidence_lower is not None else self.point_score


class RoutingRequirements(StrictModel):
    terminal: bool = True
    tools: bool = True
    minimum_context_tokens: int = Field(default=0, ge=0)
    vision: bool = False
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    minimum_input_tokens: int = Field(default=0, ge=0)
    minimum_output_tokens: int = Field(default=0, ge=0)
    structured_output: bool = False
    client_endpoint: Literal["responses"] = "responses"


class RequirementOverrides(StrictModel):
    """Sparse task requirements; null in an adjustment resets one field."""

    tools: bool | None = Field(default=None, strict=True)
    image_input: bool | None = Field(default=None, strict=True)
    image_output: bool | None = Field(default=None, strict=True)
    structured_output: bool | None = Field(default=None, strict=True)
    minimum_context_tokens: int | None = Field(default=None, ge=0, strict=True)
    minimum_output_tokens: int | None = Field(default=None, ge=0, strict=True)

    @staticmethod
    def estimated(requirements: RoutingRequirements) -> dict[str, bool | int]:
        return {
            "tools": requirements.tools,
            "image_input": requirements.vision or "image" in requirements.input_modalities,
            "image_output": "image" in requirements.output_modalities,
            "structured_output": requirements.structured_output,
            "minimum_context_tokens": requirements.minimum_context_tokens,
            "minimum_output_tokens": requirements.minimum_output_tokens,
        }

    def adjusted(
        self, patch: RequirementOverrides, estimator: RoutingRequirements
    ) -> RequirementOverrides:
        values = self.model_dump(exclude_none=True)
        estimated = self.estimated(estimator)
        for key, value in patch.model_dump(exclude_unset=True).items():
            if value is None or value == estimated[key]:
                values.pop(key, None)
            else:
                values[key] = value
        return RequirementOverrides.model_validate(values)

    def resolve(self, estimator: RoutingRequirements) -> RoutingRequirements:
        values = estimator.model_dump()
        for key, value in self.model_dump(exclude_none=True).items():
            if key in {"image_input", "image_output"}:
                direction = "input_modalities" if key == "image_input" else "output_modalities"
                modalities = [item for item in values[direction] if item != "image"]
                values[direction] = [*modalities, "image"] if value else modalities
                if key == "image_input":
                    values["vision"] = value
            else:
                values[key] = value
        return RoutingRequirements.model_validate(values)


class DecompositionItem(StrictModel):
    objective: str
    dependency_order: int = Field(ge=1)
    benchmark_id: str
    version: str
    slice_id: str
    difficulty: Difficulty
    reasoning_effort: ReasoningEffort
    risk: Risk
    passing_candidates: list[str] = Field(default_factory=list)
    competence_reduction: str
    blocked: bool = False


class AdviserExchange(StrictModel):
    requested_model: str
    actual_model: str | None = None
    actual_provider: str | None = None
    reasoning_effort: str
    request: dict[str, object]
    explanation: str
    reasoning_summary: str | None = None
    attempt: int = 1


class AdviserAnalysis(StrictModel):
    """Model-authored requirements only; provider/model selection is absent by design."""

    _exchanges: list[AdviserExchange] = PrivateAttr(default_factory=list)

    task_summary: str
    task_classification: str
    difficulty: Difficulty
    risk: Risk
    requirements: RoutingRequirements
    benchmark_requirements: list[BenchmarkSelection] = Field(min_length=1, max_length=1)
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
    difficulty: Difficulty = "normal"
    calibration_version: str = "legacy-manual-floor"
    reasoning_effort: ReasoningEffort
    risk: Risk
    evidence_policy: EvidencePolicy
    cost_quota_preference: CostQuotaPreference = "balanced"


class EstimatorPolicy(StrictModel):
    benchmark_id: str
    version: str
    slice_id: str
    minimum_common_capability: float = Field(ge=0, le=100)
    evidence_policy: EvidencePolicy = EvidencePolicy.PROVISIONAL
    reasoning_effort: ReasoningEffort


class EstimatorSelection(StrictModel):
    policy: EstimatorPolicy
    evidence_label: str = "approximate common-capability proxy"
    eligible_count: int = Field(ge=0)
    combo_name: str | None = None
    actual_provider: str | None = None
    actual_model: str | None = None


class ResourceSnapshot(StrictModel):
    eligible_configurations: int = Field(ge=0)
    eligible_routes: int = Field(ge=0)
    usable_routes: int = Field(ge=0)
    blocked_routes: int = Field(ge=0)
    unknown_routes: int = Field(ge=0)
    provider_diversity: int = Field(ge=0)
    status_coverage_percent: float = Field(ge=0, le=100)
    observed_at: str
    reset_times: list[str] = Field(default_factory=list, max_length=20)
    serialized_bytes: int = Field(ge=0)


class ResourceAdvice(StrictModel):
    action: ResourceAdviceAction
    reason: str = Field(max_length=500)
    proposed_common_floor: float | None = Field(default=None, ge=0, le=100)
    source: Literal["estimator", "deterministic_fallback"]


class CatalogueRecommendationItem(StrictModel):
    route_id: str
    provider_id: str
    displayed_model: str
    equivalence_identity: str
    reasoning_mode: str
    capability_score_central: float
    capability_score_lower: float
    capability_score_upper: float | None = None
    estimated_tier: str
    conservative_tier: str
    capability_confidence: str
    estimate_method: str
    gap_from_floor: float
    live_present: bool
    responses_callability: str
    readiness_observed_at: str | None = None
    operator_resource_class: str
    raw_cost_class: str
    alternate_route_ids: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class CatalogueModelGroup(StrictModel):
    """Display grouping only; every execution route retains its own evidence."""

    model_identity: str
    displayed_model: str
    route_count: int
    eligible_route_count: int
    configuration_count: int
    configurations: list[CatalogueRecommendationItem] = Field(default_factory=list)


class CatalogueExecutionDecision(StrictModel):
    route_id: str
    provider_id: str
    displayed_model: str
    reasoning_mode: str
    capability_score_lower: float | None = None
    equivalence_identity: str | None = None
    compatibility_basis: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)


class CatalogueExecutionSet(StrictModel):
    """Uncapped capability and effective-I/O decisions for the exposed catalogue."""

    total_evaluated: int
    eligible_count: int
    eligible: list[CatalogueExecutionDecision] = Field(default_factory=list)
    excluded: list[CatalogueExecutionDecision] = Field(default_factory=list)
    exclusion_counts: dict[str, int] = Field(default_factory=dict)


class CatalogueRecommendation(StrictModel):
    policy_version: str
    forecast_version: str
    forecast_hash: str
    readiness_version: str
    readiness_hash: str
    source_snapshot_timestamp: str
    readiness_observed_at: str | None = None
    raw_benchmark_floor: float
    common_capability_floor: float
    total_route_count: int
    live_present_count: int
    section_counts: dict[str, int]
    callable_non_codex: list[CatalogueRecommendationItem] = Field(default_factory=list)
    other_above_floor: list[CatalogueRecommendationItem] = Field(default_factory=list)
    codex_subscription_fallback: list[CatalogueRecommendationItem] = Field(default_factory=list)
    nearest_below_floor: list[CatalogueRecommendationItem] = Field(default_factory=list)
    model_groups: list[CatalogueModelGroup] = Field(default_factory=list)
    stale_warning: str | None = None
    execution_set: CatalogueExecutionSet | None = None


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
    schema_version: int = 2
    execution_options: list[ExecutionOption] = Field(default_factory=list)
    selected_execution: ExecutionOption | None = None
    execution_exclusions: dict[str, str] = Field(default_factory=dict)
    tool_free_provenance: dict[str, object] | None = None
    proposal_id: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    prompt_fingerprint: str
    workspace_summary: str
    adviser: AdviserAnalysis
    requirement_overrides: RequirementOverrides = Field(default_factory=RequirementOverrides)

    @computed_field
    @property
    def effective_requirements(self) -> RoutingRequirements:
        return self.requirement_overrides.resolve(self.adviser.requirements)

    adviser_exchanges: list[AdviserExchange] = Field(default_factory=list)
    adviser_mode: Literal["model", "local_rule", "unknown"] = "unknown"
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
    recommendation: CatalogueRecommendation | None = None
    constraint_version: int = Field(default=1, ge=1)
    estimator: EstimatorSelection | None = None
    resource_snapshot: ResourceSnapshot | None = None
    resource_advice: ResourceAdvice | None = None


class ProposalCreateRequest(StrictModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    workspace_summary: str = Field(default="", max_length=20_000)
    estimator_policy: EstimatorPolicy | None = None


class ProposalAdjustmentRequest(StrictModel):
    requirement_overrides: RequirementOverrides | None = None
    benchmark_id: str | None = None
    version: str | None = None
    slice_id: str | None = None
    difficulty: Difficulty | None = None
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
