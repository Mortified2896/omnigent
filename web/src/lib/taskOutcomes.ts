// Typed client for the `/v1/task-runs/...` and
// `/v1/sessions/{id}/task-runs[/...]` endpoints introduced with
// the task-outcome tracking vertical slice. Mirrors
// `omnigent/server/routes/task_outcomes.py`.
//
// All requests go through the existing Vite `/v1` proxy
// (`web/vite.config.ts`) so no proxy changes are needed.

import { authenticatedFetch } from "./identity";
import type { Bubble } from "./renderItems";

/**
 * Typed error raised by the task-run fetchers below. Carries the
 * HTTP status so callers can distinguish a not-yet-indexed
 * ``404`` (retryable) from a real server fault.
 */
export class TaskRunFetchError extends Error {
  readonly status: number;
  constructor(status: number, message?: string) {
    super(message ?? `task run fetch failed: ${status}`);
    this.name = "TaskRunFetchError";
    this.status = status;
  }
}

/**
 * A run can transition through several readiness phases before the
 * operator can act on it:
 *
 * - ``loading`` — initial fetch in flight.
 * - ``waiting`` — last fetch returned retryable (404 or evaluation
 *   still null); backing off before the next attempt.
 * - ``ready`` — run found and evaluation (or human review) present.
 * - ``exhausted`` — retry budget spent without an evaluation. The
 *   component should render a compact retry surface, not pretend
 *   the automated evaluator returned ``unsure``.
 * - ``failed`` — non-retryable error (5xx, network, etc).
 */
export type TaskRunReadinessPhase = "loading" | "waiting" | "ready" | "exhausted" | "failed";

/** Backoff schedule (ms) for follow-up readiness polls. */
export const TASK_RUN_READINESS_DELAYS_MS: readonly number[] = [800, 1_600, 3_200, 6_400] as const;

/** Maximum number of follow-up attempts after the initial fetch. */
export const TASK_RUN_READINESS_MAX_ATTEMPTS = TASK_RUN_READINESS_DELAYS_MS.length;

/**
 * Select the single transcript position that owns an outcome card for each
 * response. A reconnect can temporarily leave the live and hydrated copies
 * of a response in the bubble list; the final occurrence is the canonical
 * position, directly below the completed assistant result.
 */
export function canonicalOutcomeResponseIds(bubbles: readonly Bubble[]): ReadonlySet<string> {
  const ids = new Set<string>();
  for (let index = bubbles.length - 1; index >= 0; index -= 1) {
    const bubble = bubbles[index];
    if (bubble?.kind === "assistant" && bubble.lifecycle !== "streaming") {
      ids.add(bubble.responseId);
    }
  }
  return ids;
}

export function ownsOutcomeCard(bubbles: readonly Bubble[], bubbleIndex: number): boolean {
  const bubble = bubbles[bubbleIndex];
  if (!bubble || bubble.kind !== "assistant" || bubble.lifecycle === "streaming") return false;
  return !bubbles.some(
    (candidate, index) =>
      index > bubbleIndex &&
      candidate.kind === "assistant" &&
      candidate.lifecycle !== "streaming" &&
      candidate.responseId === bubble.responseId,
  );
}

/** A single task run record as returned by the server. */
export interface TaskRun {
  id: string;
  conversation_id: string;
  response_id: string | null;
  triggering_message_id: string | null;
  project_path: string | null;
  task_description: string | null;
  proposed_task_family: string | null;
  estimated_difficulty: string | null;
  harness_id: string | null;
  requested_route_id: string | null;
  selected_provider: string | null;
  selected_model: string | null;
  reasoning_effort: string | null;
  permission_mode: string | null;
  omniroute_decision_id: string | null;
  selection_strategy: string | null;
  billing_class: string | null;
  fallback_used: boolean | null;
  terminal_status: "running" | "completed" | "failed" | "cancelled" | "incomplete";
  started_at: number | null;
  terminal_at: number | null;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_cost_usd: number | null;
  response_summary: string | null;
  changed_files: string[] | null;
  commit_sha: string | null;
  failure_error_code: string | null;
  failure_error_message: string | null;
  langfuse_trace_id: string | null;
  langfuse_observation_id: string | null;
  created_at: number;
  updated_at: number;
}

/** Lighter-weight summary used by the listing endpoints. */
export interface TaskRunSummary {
  id: string;
  conversation_id: string;
  response_id: string | null;
  terminal_status: TaskRun["terminal_status"];
  started_at: number | null;
  terminal_at: number | null;
  duration_ms: number | null;
  selected_provider: string | null;
  selected_model: string | null;
  requested_route_id: string | null;
  fallback_used: boolean | null;
  harness_id: string | null;
  proposed_task_family: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_cost_usd: number | null;
  commit_sha: string | null;
  changed_files_count: number | null;
  failure_error_code: string | null;
  langfuse_trace_id: string | null;
  created_at: number;
  updated_at: number;
}

/** A single automated evaluation record (one per task run). */
export interface TaskEvaluation {
  id: string;
  task_run_id: string;
  evaluator_type: "deterministic" | "llm";
  evaluator_provider: string | null;
  evaluator_model: string | null;
  evaluator_route_id: string | null;
  verdict: "success" | "partial" | "failure" | "inconclusive";
  confidence: number | null;
  quality_score: number | null;
  proposed_task_family: string | null;
  reasoning: string | null;
  evidence: string[] | null;
  unresolved_issues: string[] | null;
  created_at: number;
}

/** A human review (upserted on re-submit). */
export interface TaskReview {
  id: string;
  task_run_id: string;
  verdict: "success" | "partial" | "failure" | "unsure" | "skipped";
  quality_score: number | null;
  final_task_family: string | null;
  evaluator_accuracy: "correct" | "partly_correct" | "incorrect" | "unsure" | null;
  comments: string | null;
  created_by: string | null;
  review_action: "accepted" | "adjusted" | "declined" | null;
  learning_eligible: boolean;
  route_fit: "appropriate" | "too_weak" | "overkill" | "wrong_capability" | "unsure" | null;
  failure_attribution: string | null;
  preferred_route_id: string | null;
  preferred_reasoning_effort: string | null;
  source_evaluation_id: string | null;
  review_schema_version: number;
  created_at: number;
  updated_at: number;
}

/** Aggregate response of `GET /v1/task-runs/{id}`. */
export interface TaskRunDetailResponse {
  run: TaskRun;
  evaluation: TaskEvaluation | null;
  /** Review submitted by the requesting user (``null`` when none). */
  review: TaskReview | null;
  /** Any review (any reviewer) — for the unreviewed-runs check. */
  any_review: TaskReview | null;
  langfuse_pending: boolean;
}

/** Stable list of task families surfaced in the picker. */
export const TASK_FAMILIES: readonly string[] = [
  "repository_inspection",
  "planning",
  "small_bug_fix",
  "feature_implementation",
  "test_failure_repair",
  "refactor",
  "frontend",
  "backend_api",
  "database_migration",
  "infrastructure_config",
  "code_review",
  "documentation",
  "other",
] as const;

/** Stable list of review verdicts the picker accepts. */
export const REVIEW_VERDICTS: readonly TaskReview["verdict"][] = [
  "success",
  "partial",
  "failure",
  "unsure",
  "skipped",
] as const;

/** Stable list of "evaluator accuracy" verdicts the picker accepts. */
export const EVALUATOR_ACCURACY_VALUES: readonly NonNullable<TaskReview["evaluator_accuracy"]>[] = [
  "correct",
  "partly_correct",
  "incorrect",
  "unsure",
] as const;

/** Body of `POST /v1/task-runs/{id}/review`. */
export interface UpsertReviewRequest {
  action?: "accept" | "adjust" | "decline";
  source_evaluation_id?: string | null;
  verdict?: TaskReview["verdict"];
  route_fit?: TaskReview["route_fit"];
  failure_attribution?: string | null;
  preferred_route_id?: string | null;
  preferred_reasoning_effort?: string | null;
  quality_score?: number | null;
  final_task_family?: string | null;
  evaluator_accuracy?: TaskReview["evaluator_accuracy"];
  comments?: string | null;
}

/** Response of `GET /v1/sessions/{id}/task-runs`. */
export interface ListSessionTaskRunsResponse {
  object: "list";
  runs: TaskRunSummary[];
}

/** Response of `GET /v1/sessions/{id}/unreviewed-task-outcomes`. */
export interface ListUnreviewedTaskOutcomesResponse {
  object: "list";
  task_run_ids: string[];
  runs: TaskRunSummary[];
}

export async function listSessionTaskRuns(
  sessionId: string,
  limit: number = 50,
): Promise<ListSessionTaskRunsResponse> {
  const url = `/v1/sessions/${encodeURIComponent(sessionId)}/task-runs?limit=${limit}`;
  const resp = await authenticatedFetch(url, { credentials: "same-origin" });
  if (!resp.ok) {
    throw new Error(`listSessionTaskRuns failed: ${resp.status}`);
  }
  return resp.json();
}

export async function listUnreviewedTaskOutcomes(
  sessionId: string,
  limit: number = 100,
): Promise<ListUnreviewedTaskOutcomesResponse> {
  const url = `/v1/sessions/${encodeURIComponent(sessionId)}/unreviewed-task-outcomes?limit=${limit}`;
  const resp = await authenticatedFetch(url, { credentials: "same-origin" });
  if (!resp.ok) {
    throw new Error(`listUnreviewedTaskOutcomes failed: ${resp.status}`);
  }
  return resp.json();
}

export async function getTaskRunForResponse(
  sessionId: string,
  responseId: string,
  signal?: AbortSignal,
): Promise<TaskRunDetailResponse> {
  const url = `/v1/sessions/${encodeURIComponent(sessionId)}/task-runs/by-response/${encodeURIComponent(responseId)}`;
  const resp = await authenticatedFetch(url, {
    credentials: "same-origin",
    ...(signal ? { signal } : {}),
  });
  if (!resp.ok) throw new TaskRunFetchError(resp.status);
  return resp.json();
}

export async function getTaskRun(taskRunId: string): Promise<TaskRunDetailResponse> {
  const url = `/v1/task-runs/${encodeURIComponent(taskRunId)}`;
  const resp = await authenticatedFetch(url, { credentials: "same-origin" });
  if (!resp.ok) {
    throw new TaskRunFetchError(resp.status, `getTaskRun failed: ${resp.status}`);
  }
  return resp.json();
}

export async function submitTaskRunReview(
  taskRunId: string,
  body: UpsertReviewRequest,
): Promise<TaskReview> {
  const url = `/v1/task-runs/${encodeURIComponent(taskRunId)}/review`;
  const resp = await authenticatedFetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new TaskRunFetchError(
      resp.status,
      `submitTaskRunReview failed: ${resp.status} ${text.slice(0, 200)}`,
    );
  }
  return resp.json();
}

/**
 * Result of {@link useTaskRunForResponse}. The component renders
 * against these fields instead of wiring its own timers.
 */
export interface TaskRunReadiness {
  phase: TaskRunReadinessPhase;
  detail: TaskRunDetailResponse | null;
  error: string | null;
  /** Restart the bounded polling cycle (also used by the [Retry] button). */
  retry: () => void;
}
