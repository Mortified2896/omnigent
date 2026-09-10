import { authenticatedFetch } from "./identity";

export type O3EvidenceClass = "exact" | "proxy" | "advisory" | "forecast" | "unknown";
export type O3Difficulty = "easy" | "normal" | "moderate" | "hard" | "frontier";
export type O3EvidencePolicy = "strict" | "provisional";
export type O3Disposition = "route" | "borderline" | "decompose" | "defer";
export type O3DecisionAction = "approve" | "decline" | "defer" | "wait" | "run_anyway";
export type O3CandidateStatus = "pass" | "provisional" | "excluded";

export interface O3BenchmarkSlice {
  benchmark_id: string;
  version: string;
  slice_id: string;
  label: string;
  interpretation: string;
  task_ids: string[];
  task_manifest_digest: string;
  official: boolean;
}

export interface O3BenchmarkRequirement {
  benchmark_id: string;
  version: string;
  slice_id: string;
  minimum_score: number;
  reason: string;
  difficulty: O3Difficulty | null;
  calibration_version: string | null;
}

export type O3BenchmarkSelection = Omit<
  O3BenchmarkRequirement,
  "minimum_score" | "difficulty" | "calibration_version"
>;

export interface O3BenchmarkEvidence {
  benchmark_id: string;
  benchmark_version: string;
  slice_id: string;
  harness: string;
  harness_version: string | null;
  model: string;
  provider_path: string | null;
  reasoning_effort: string;
  point_score: number;
  confidence_lower: number | null;
  confidence_upper: number | null;
  number_of_tasks: number;
  number_of_attempts: number;
  evidence_class: O3EvidenceClass;
  source_type: string;
  source_reference: string;
  evaluation_date: string;
}

export interface O3CandidateSnapshot {
  candidate_id: string;
  provider_id: string;
  model: string;
  catalogue_model_id: string;
  harness: string;
  supported_reasoning_efforts: string[];
  context_tokens: number | null;
  terminal: boolean;
  tools: boolean;
  vision: boolean;
  responses_api: boolean;
  monetary_cost_usd: number | null;
  cost_source: string;
  quota_source: string;
  last_full_probe_at: string;
  probe_reference: string;
  provider_usable: boolean;
  model_present: boolean;
  quota_available: boolean | null;
  quota_remaining_percent: number | null;
  quota_reset_at: string | null;
  recent_success_rate: number | null;
  recent_retry_rate: number | null;
  latency_ms: number | null;
}

export interface O3RankingInputs {
  evidence_confidence: number;
  competence_margin: number | null;
  health: number;
  estimated_monetary_cost_usd: number | null;
  quota_remaining_percent: number | null;
  quota_reset_at: string | null;
  quota_scarcity_penalty: number;
  recent_failure_rate: number | null;
  recent_retry_rate: number | null;
  latency_ms: number | null;
  deterministic_score: number;
}

export interface O3CandidateEvaluation {
  candidate: O3CandidateSnapshot;
  status: O3CandidateStatus;
  evidence_class: O3EvidenceClass;
  admission_score: number | null;
  evidence: O3BenchmarkEvidence[];
  exclusions: string[];
  caveats: string[];
  ranking: O3RankingInputs | null;
}

export interface O3DecompositionItem {
  objective: string;
  dependency_order: number;
  benchmark_id: string;
  version: string;
  slice_id: string;
  difficulty: O3Difficulty;
  reasoning_effort: string;
  risk: string;
  passing_candidates: string[];
  competence_reduction: string;
  blocked: boolean;
}

export interface O3ExecutionProvenance {
  call_log_id: string;
  timestamp: string;
  path: string;
  method: string;
  session_tag: string;
  combo_name: string | null;
  requested_model: string | null;
  provider: string;
  model: string;
  connection_id: string | null;
  correlation_id: string | null;
  http_status: number;
  duration_ms: number | null;
  token_usage: {
    input_tokens: number | null;
    output_tokens: number | null;
    reasoning_tokens: number | null;
    cache_read_tokens: number | null;
    cache_write_tokens: number | null;
  };
  reasoning_effort: string | null;
  estimated_cost_usd: number | null;
}

export interface O3RequirementOverrides {
  tools?: boolean | null;
  image_input?: boolean | null;
  image_output?: boolean | null;
  structured_output?: boolean | null;
  minimum_context_tokens?: number | null;
  minimum_output_tokens?: number | null;
}

export interface O3RoutingProposal {
  requirement_overrides?: O3RequirementOverrides;
  effective_requirements?: O3RoutingProposal["adviser"]["requirements"];
  schema_version: number;
  proposal_id: string;
  created_at: string;
  updated_at: string;
  expires_at: string;
  prompt_fingerprint: string;
  workspace_summary: string;
  adviser_mode?: "model" | "local_rule" | "unknown";
  adviser_exchanges?: {
    requested_model: string;
    actual_model: string | null;
    actual_provider: string | null;
    reasoning_effort: string;
    request: Record<string, unknown>;
    explanation: string;
    reasoning_summary: string | null;
    attempt: number;
  }[];
  adviser: {
    task_summary: string;
    task_classification: string;
    difficulty: O3Difficulty;
    risk: string;
    requirements: {
      terminal: boolean;
      tools: boolean;
      minimum_context_tokens: number;
      vision: boolean;
      input_modalities?: string[];
      output_modalities?: string[];
      minimum_input_tokens?: number;
      minimum_output_tokens?: number;
      structured_output?: boolean;
      client_endpoint?: "responses";
    };
    benchmark_requirements: O3BenchmarkSelection[];
    proposed_reasoning_effort: string;
    evidence_policy: O3EvidencePolicy;
    disposition: O3Disposition;
    confidence: number;
    rationale: string;
    decomposition: O3DecompositionItem[];
  };
  approved_constraints: {
    benchmark: O3BenchmarkRequirement;
    difficulty: O3Difficulty;
    calibration_version: string;
    reasoning_effort: string;
    risk: string;
    evidence_policy: O3EvidencePolicy;
    cost_quota_preference: string;
  };
  evaluations: O3CandidateEvaluation[];
  frontier: {
    requested_minimum: number;
    global_measured_frontier: number | null;
    accessible_configured_frontier: number | null;
    healthy_available_frontier: number | null;
    passing_exact_candidates: string[];
    provisional_candidates: string[];
    capability_gap: string | null;
  };
  disposition: O3Disposition;
  decision: O3DecisionAction | null;
  decision_reason: string | null;
  execution_options?: O3ExecutionOption[];
  selected_execution?: O3ExecutionOption | null;
  derived_combo_name: string | null;
  derived_combo_definition: Record<string, unknown> | null;
  session_id: string | null;
  actual_provider: string | null;
  actual_model: string | null;
  actual_reasoning_effort: string | null;
  execution_provenance: O3ExecutionProvenance[];
  execution_status: string | null;
  provenance_synced_at: string | null;
  task_outcome: string | null;
  terminal_disposition: string | null;
  recommendation?: O3CatalogueRecommendation | null;
  constraint_version?: number;
  estimator?: {
    policy: O3EstimatorPolicy;
    evidence_label: string;
    eligible_count: number;
    combo_name: string | null;
    actual_provider: string | null;
    actual_model: string | null;
  } | null;
  resource_snapshot?: {
    eligible_configurations: number;
    eligible_routes: number;
    usable_routes: number;
    blocked_routes: number;
    unknown_routes: number;
    provider_diversity: number;
    status_coverage_percent: number;
    observed_at: string;
    reset_times: string[];
    serialized_bytes: number;
  } | null;
  resource_advice?: {
    action: "start_now" | "wait" | "ask_to_lower_floor";
    reason: string;
    proposed_common_floor: number | null;
    source: "estimator" | "deterministic_fallback";
  } | null;
}

export interface O3CatalogueRecommendationItem {
  route_id: string;
  provider_id: string;
  displayed_model: string;
  equivalence_identity: string;
  reasoning_mode: string;
  capability_score_central: number;
  capability_score_lower: number;
  capability_score_upper: number | null;
  estimated_tier: string;
  conservative_tier: string;
  capability_confidence: string;
  estimate_method: string;
  gap_from_floor: number;
  live_present: boolean;
  responses_callability: string;
  readiness_observed_at: string | null;
  operator_resource_class: string;
  raw_cost_class: string;
  alternate_route_ids: string[];
  caveats: string[];
}

export interface O3CatalogueRecommendation {
  policy_version: string;
  forecast_version: string;
  forecast_hash: string;
  readiness_version: string;
  readiness_hash: string;
  source_snapshot_timestamp: string;
  readiness_observed_at: string | null;
  raw_benchmark_floor: number;
  common_capability_floor: number;
  total_route_count: number;
  live_present_count: number;
  section_counts: Record<string, number>;
  callable_non_codex: O3CatalogueRecommendationItem[];
  other_above_floor: O3CatalogueRecommendationItem[];
  codex_subscription_fallback: O3CatalogueRecommendationItem[];
  nearest_below_floor: O3CatalogueRecommendationItem[];
  model_groups?: {
    model_identity: string;
    displayed_model: string;
    route_count: number;
    eligible_route_count: number;
    configuration_count: number;
    configurations: O3CatalogueRecommendationItem[];
  }[];
  stale_warning: string | null;
  execution_set?: O3CatalogueExecutionSet | null;
}

export interface O3CatalogueExecutionDecision {
  route_id: string;
  provider_id: string;
  displayed_model: string;
  reasoning_mode: string;
  capability_score_lower: number | null;
  equivalence_identity: string | null;
  compatibility_basis: string[];
  exclusions: string[];
}

export interface O3CatalogueExecutionSet {
  total_evaluated: number;
  eligible_count: number;
  eligible: O3CatalogueExecutionDecision[];
  excluded: O3CatalogueExecutionDecision[];
  exclusion_counts: Record<string, number>;
}

export interface O3ProposalAdjustment {
  requirement_overrides?: O3RequirementOverrides;
  benchmark_id?: string;
  version?: string;
  slice_id?: string;
  minimum_score?: number;
  difficulty?: O3Difficulty;
  reasoning_effort?: string;
  risk?: string;
  evidence_policy?: O3EvidencePolicy;
  cost_quota_preference?: string;
}

export interface O3EstimatorPolicy {
  benchmark_id: string;
  version: string;
  slice_id: string;
  minimum_common_capability: number;
  evidence_policy: O3EvidencePolicy;
  reasoning_effort: string;
}

export interface O3ProposalDecision {
  action: O3DecisionAction;
  acknowledge_provisional?: boolean;
  confirm_run_anyway?: boolean;
  reason?: string;
}

export interface O3RoutingRegistry {
  source_pool: string;
  slices: O3BenchmarkSlice[];
}

export class O3RoutingReviewRequestError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "O3RoutingReviewRequestError";
    this.status = status;
    this.code = code;
  }
}

async function routingRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, init);
  if (response.ok) return (await response.json()) as T;
  let message = `${response.status} ${response.statusText}`.trim();
  let code: string | null = null;
  try {
    const body = (await response.json()) as {
      error?: string | { code?: string; message?: string };
      detail?: string;
    };
    if (typeof body.error === "string") message = body.error;
    else if (typeof body.error === "object" && body.error !== null) {
      if (typeof body.error.message === "string") message = body.error.message;
      if (typeof body.error.code === "string") code = body.error.code;
    } else if (typeof body.detail === "string") message = body.detail;
  } catch {
    // Preserve the status fallback for non-JSON errors.
  }
  throw new O3RoutingReviewRequestError(
    message || "Routing review request failed",
    response.status,
    code,
  );
}

function jsonMutation(method: "POST" | "PATCH", body: object): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export function createO3RoutingProposal(
  prompt: string,
  workspaceSummary: string,
  estimatorPolicy?: O3EstimatorPolicy,
): Promise<O3RoutingProposal> {
  return routingRequest(
    "/v1/o3/routing-review/proposals",
    jsonMutation("POST", {
      prompt,
      workspace_summary: workspaceSummary,
      ...(estimatorPolicy ? { estimator_policy: estimatorPolicy } : {}),
    }),
  );
}

export function getO3RoutingProposal(proposalId: string): Promise<O3RoutingProposal> {
  return routingRequest(`/v1/o3/routing-review/proposals/${encodeURIComponent(proposalId)}`);
}

export function getO3RoutingRegistry(): Promise<O3RoutingRegistry> {
  return routingRequest("/v1/o3/routing-review/registry");
}

export function adjustO3RoutingProposal(
  proposalId: string,
  adjustment: O3ProposalAdjustment,
): Promise<O3RoutingProposal> {
  return routingRequest(
    `/v1/o3/routing-review/proposals/${encodeURIComponent(proposalId)}`,
    jsonMutation("PATCH", adjustment),
  );
}

export function decideO3RoutingProposal(
  proposalId: string,
  decision: O3ProposalDecision,
): Promise<O3RoutingProposal> {
  return routingRequest(
    `/v1/o3/routing-review/proposals/${encodeURIComponent(proposalId)}/decision`,
    jsonMutation("POST", decision),
  );
}

export function linkO3RoutingProposalSession(
  proposalId: string,
  sessionId: string,
): Promise<O3RoutingProposal> {
  return routingRequest(
    `/v1/o3/routing-review/proposals/${encodeURIComponent(proposalId)}/session`,
    jsonMutation("POST", { session_id: sessionId }),
  );
}

const O3_DRAFT_KEY = "omnigent:o3-routing-review:draft:v1";
const O3_ESTIMATOR_POLICY_KEY = "omnigent:o3-routing-review:estimator-policy:v1";

export function readO3EstimatorPolicy(): O3EstimatorPolicy | null {
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(O3_ESTIMATOR_POLICY_KEY) ?? "null",
    ) as Partial<O3EstimatorPolicy> | null;
    return parsed &&
      typeof parsed.benchmark_id === "string" &&
      typeof parsed.version === "string" &&
      typeof parsed.slice_id === "string" &&
      typeof parsed.minimum_common_capability === "number" &&
      (parsed.evidence_policy === "strict" || parsed.evidence_policy === "provisional") &&
      typeof parsed.reasoning_effort === "string"
      ? (parsed as O3EstimatorPolicy)
      : null;
  } catch {
    return null;
  }
}

export function writeO3EstimatorPolicy(policy: O3EstimatorPolicy): void {
  try {
    window.localStorage.setItem(O3_ESTIMATOR_POLICY_KEY, JSON.stringify(policy));
  } catch {
    // The selected policy remains active in memory when storage is unavailable.
  }
}

export interface O3RoutingDraft {
  version: 1;
  proposalId: string;
  proposalUpdatedAt: string;
  initialPrompt: string;
  composerMessage: string;
  workspaceSummary: string;
  sessionId: string | null;
}

function isRoutingDraft(value: unknown): value is O3RoutingDraft {
  if (typeof value !== "object" || value === null) return false;
  const draft = value as Partial<O3RoutingDraft>;
  return (
    draft.version === 1 &&
    typeof draft.proposalId === "string" &&
    typeof draft.proposalUpdatedAt === "string" &&
    typeof draft.initialPrompt === "string" &&
    typeof draft.composerMessage === "string" &&
    typeof draft.workspaceSummary === "string" &&
    (draft.sessionId === null || typeof draft.sessionId === "string")
  );
}

export function readO3RoutingDraft(): O3RoutingDraft | null {
  try {
    const raw = window.localStorage.getItem(O3_DRAFT_KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    return isRoutingDraft(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function writeO3RoutingDraft(draft: O3RoutingDraft): void {
  try {
    window.localStorage.setItem(O3_DRAFT_KEY, JSON.stringify(draft));
  } catch {
    // Storage can be unavailable in privacy modes; the in-memory proposal still works.
  }
}

export function clearO3RoutingDraft(): void {
  try {
    window.localStorage.removeItem(O3_DRAFT_KEY);
  } catch {
    // Best-effort cleanup only.
  }
}

export function routingDraftForProposal(
  proposal: O3RoutingProposal,
  initialPrompt: string,
  composerMessage: string,
  workspaceSummary: string,
  sessionId: string | null = null,
): O3RoutingDraft {
  return {
    version: 1,
    proposalId: proposal.proposal_id,
    proposalUpdatedAt: proposal.updated_at,
    initialPrompt,
    composerMessage,
    workspaceSummary,
    sessionId,
  };
}

export interface O3ExecutionOption {
  mode: "tool_capable_native" | "hard_tool_free";
  route: string;
  provider: string;
  cost_class: string;
  capability_score_lower: number;
  reason: string;
}
