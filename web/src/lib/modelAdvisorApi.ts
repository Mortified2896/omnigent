/** Typed client for the model-advisor API. Owner identity, assignment and
 * round state are always server-derived; the browser sends only ids, the
 * task text and launch context. Conflicts surface as AdvisorConflictError.
 */
import { authenticatedFetch } from "@/lib/identity";
import type { ReviewView } from "@/model-advisor/ModelAdvisorPanel";
import type { AdvisorOption, AdvisorPreferences, SavedPreferences } from "@/model-advisor/editor";

interface ErrorBody {
  detail?: unknown;
  error?: { code?: string; message?: string };
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as ErrorBody;
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      const first = body.detail[0] as { msg?: string } | undefined;
      if (first && typeof first.msg === "string") return first.msg;
    }
    if (body.error?.message) return body.error.message;
  } catch {
    // Fall through to the status text.
  }
  return response.statusText || `Request failed (${response.status})`;
}

/** Raised on 409/412 CAS conflicts; the caller must reload, never retry blind. */
export class AdvisorConflictError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "AdvisorConflictError";
    this.status = status;
  }
}

async function advisorFetch<T>(input: string, init: RequestInit): Promise<T> {
  const response = await authenticatedFetch(input, init);
  if (!response.ok) {
    const message = await parseError(response);
    if (response.status === 409 || response.status === 412) {
      throw new AdvisorConflictError(response.status, message);
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export interface CatalogOptionDto {
  candidate_id: string;
  model_id: string;
  display_name: string;
  lane_id: AdvisorOption["lane_id"];
  reasoning_effort: string;
  access_class: "chatgpt_plan" | "glm_plan";
  is_default_effort: boolean;
}

export interface CatalogDto {
  object: "model_advisor.catalog";
  catalog_revision: string;
  options: CatalogOptionDto[];
}

export interface PreferencesDto {
  object: "model_advisor.preferences";
  version: number;
  etag: string | null;
  state: string;
  preferences: AdvisorPreferences | null;
}

export interface RoundReviewDto {
  round_fingerprint: string;
  human_candidate_id: string;
  advisor_candidate_id: string;
  rationale: string;
  assigned_candidate_id: string;
  assigned_arm: "human" | "advisor" | "same";
  human_probability_percent: number;
  overridden: boolean;
  override_reason: string | null;
  comparison_group: string;
}

export interface RoundDto {
  object: "model_advisor.round";
  round_id: string;
  state: string;
  version: number;
  etag: string;
  failure_reason: string | null;
  review?: RoundReviewDto;
  execution: { session_id: string | null; uncertain: boolean };
  requested_execution?: {
    session_id: string;
    model: string | null;
    reasoning_effort: string | null;
    access_lane: string | null;
    comparison_group: string | null;
  };
  actual_execution?: {
    status: "unknown" | "observed";
    reason?: string;
    provider?: string | null;
    model?: string | null;
    reasoning_effort?: string | null;
    access_lane?: string | null;
  };
  advisor_overhead?: {
    latency_ms: number | null;
    input_tokens: number | null;
    output_tokens: number | null;
    cached_input_tokens: number | null;
    response_id: string | null;
  };
  launch_error?: string;
}

export async function fetchCatalog(hostId: string): Promise<CatalogDto> {
  return advisorFetch<CatalogDto>(
    `/v1/model-advisor/catalog?host_id=${encodeURIComponent(hostId)}`,
    { method: "GET" },
  );
}

export async function fetchPreferences(
  hostId: string,
  profile = "default",
): Promise<PreferencesDto> {
  return advisorFetch<PreferencesDto>(
    `/v1/model-advisor/preferences?host_id=${encodeURIComponent(hostId)}&profile=${encodeURIComponent(profile)}`,
    { method: "GET" },
  );
}

export async function savePreferences(
  hostId: string,
  profile: string,
  preferences: AdvisorPreferences,
  expectedVersion: number,
): Promise<PreferencesDto> {
  return advisorFetch<PreferencesDto>("/v1/model-advisor/preferences", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host_id: hostId,
      profile,
      expected_version: expectedVersion,
      preferences: {
        enabled: preferences.enabled,
        allowed_candidate_ids: preferences.allowed_candidate_ids,
        advisor_candidate_id: preferences.advisor_candidate_id,
        human_probability_percent: preferences.human_probability_percent,
      },
    }),
  });
}

export async function createRound(
  hostId: string,
  profile: string,
  task: string,
  humanCandidateId: string,
  preferences: AdvisorPreferences,
  submissionKey: string,
): Promise<RoundDto> {
  return advisorFetch<RoundDto>("/v1/model-advisor/rounds", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host_id: hostId,
      profile,
      task,
      human_candidate_id: humanCandidateId,
      submission_key: submissionKey,
      preferences: {
        enabled: preferences.enabled,
        allowed_candidate_ids: preferences.allowed_candidate_ids,
        advisor_candidate_id: preferences.advisor_candidate_id,
        human_probability_percent: preferences.human_probability_percent,
      },
    }),
  });
}

export async function fetchRound(hostId: string, roundId: string): Promise<RoundDto> {
  return advisorFetch<RoundDto>(
    `/v1/model-advisor/rounds/${encodeURIComponent(roundId)}?host_id=${encodeURIComponent(hostId)}`,
    { method: "GET" },
  );
}

export interface RoundLaunchParams {
  agent_id: string;
  workspace: string;
  terminal_launch_args?: string[] | null;
}

export async function confirmRound(
  hostId: string,
  roundId: string,
  expectedVersion: number,
  launch: RoundLaunchParams,
  overrideCandidateId: string | null,
  reason: string | null,
): Promise<RoundDto> {
  return advisorFetch<RoundDto>(`/v1/model-advisor/rounds/${encodeURIComponent(roundId)}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host_id: hostId,
      expected_version: expectedVersion,
      override_candidate_id: overrideCandidateId,
      reason,
      launch,
    }),
  });
}

export async function cancelRound(
  hostId: string,
  roundId: string,
  expectedVersion: number,
): Promise<RoundDto> {
  return advisorFetch<RoundDto>(`/v1/model-advisor/rounds/${encodeURIComponent(roundId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host_id: hostId, expected_version: expectedVersion }),
  });
}

/** Map a server catalog projection into the editor's option shape.
 * Availability truth lives server-side: every projected option here was
 * qualified by the live catalog, so it is available by construction.
 */
export function toAdvisorOptions(catalog: CatalogDto): AdvisorOption[] {
  return catalog.options.map((option) => ({
    candidate_id: option.candidate_id,
    model_id: option.model_id,
    display_name: option.display_name,
    lane_id: option.lane_id,
    reasoning_effort: option.reasoning_effort,
    available: true,
  }));
}

/** Bridge a server round projection into the review view's props. */
export function toReviewView(round: RoundDto): ReviewView | null {
  if (!round.review) return null;
  return {
    round_fingerprint: round.review.round_fingerprint,
    human_candidate_id: round.review.human_candidate_id,
    advisor_candidate_id: round.review.advisor_candidate_id,
    rationale: round.review.rationale,
    assigned_candidate_id: round.review.assigned_candidate_id,
    assigned_arm: round.review.assigned_arm,
    human_probability_percent: round.review.human_probability_percent,
  };
}

export function toSavedPreferences(dto: PreferencesDto): SavedPreferences | null {
  if (dto.preferences === null || dto.etag === null) return null;
  return { version: dto.version, etag: dto.etag, preferences: dto.preferences };
}
