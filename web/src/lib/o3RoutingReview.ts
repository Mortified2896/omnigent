import { authenticatedFetch } from "./identity";

export type O3EvidenceClass = "exact" | "proxy" | "advisory" | "unknown";
export type O3EvidencePolicy = "strict" | "provisional";
export type O3Disposition = "route" | "borderline" | "decompose" | "defer";
export type O3DecisionAction = "approve" | "decline" | "defer" | "run_anyway";
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
}

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
  minimum_score: number;
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

export interface O3RoutingProposal {
  schema_version: number;
  proposal_id: string;
  created_at: string;
  updated_at: string;
  expires_at: string;
  prompt_fingerprint: string;
  workspace_summary: string;
  adviser: {
    task_summary: string;
    task_classification: string;
    difficulty: string;
    risk: string;
    requirements: {
      terminal: boolean;
      tools: boolean;
      minimum_context_tokens: number;
      vision: boolean;
    };
    benchmark_requirements: O3BenchmarkRequirement[];
    proposed_reasoning_effort: string;
    evidence_policy: O3EvidencePolicy;
    disposition: O3Disposition;
    confidence: number;
    rationale: string;
    decomposition: O3DecompositionItem[];
  };
  approved_constraints: {
    benchmark: O3BenchmarkRequirement;
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
}

export interface O3ProposalAdjustment {
  benchmark_id?: string;
  version?: string;
  slice_id?: string;
  minimum_score?: number;
  reasoning_effort?: string;
  risk?: string;
  evidence_policy?: O3EvidencePolicy;
  cost_quota_preference?: string;
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

async function routingRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, init);
  if (response.ok) return (await response.json()) as T;
  let message = `${response.status} ${response.statusText}`.trim();
  try {
    const body = (await response.json()) as {
      error?: string | { message?: string };
      detail?: string;
    };
    if (typeof body.error === "string") message = body.error;
    else if (typeof body.error?.message === "string") message = body.error.message;
    else if (typeof body.detail === "string") message = body.detail;
  } catch {
    // Preserve the status fallback for non-JSON errors.
  }
  throw new Error(message || "Routing review request failed");
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
): Promise<O3RoutingProposal> {
  return routingRequest(
    "/v1/o3/routing-review/proposals",
    jsonMutation("POST", { prompt, workspace_summary: workspaceSummary }),
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
