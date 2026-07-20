// Typed client for the `/v1/task-runs/...` and
// `/v1/sessions/{id}/task-runs[/...]` endpoints introduced with
// the task-outcome tracking vertical slice. Mirrors
// `omnigent/server/routes/task_outcomes.py`.
//
// All requests go through the existing Vite `/v1` proxy
// (`web/vite.config.ts`) so no proxy changes are needed.

import { useEffect, useMemo, useRef, useState } from "react";
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

/** The endpoint returned a run for another transcript response. */
export class TaskRunResponseIdentityError extends TaskRunFetchError {
  constructor(requestedResponseId: string, returnedResponseId: string | null) {
    super(409, "task run response identity mismatch");
    console.warn("Task outcome response identity mismatch", {
      requestedResponseId,
      returnedResponseId,
    });
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

/** Session-level discovery retries after a turn becomes idle. */
export const TASK_RUN_DISCOVERY_DELAYS_MS: readonly number[] = [800, 1_600, 3_200, 6_400] as const;

/** Backoff schedule (ms) for evaluation readiness polls. */
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

/**
 * Returns true if the given bubble has visible final text content that
 * qualifies it as a "final answer" eligible to own a task outcome card.
 *
 * A reasoning-only bubble, tool-only bubble, or bubble with no text content
 * does NOT qualify as the final answer.
 */
export function hasVisibleFinalText(bubble: Bubble): boolean {
  if (bubble.kind !== "assistant") return false;
  return bubble.items.some(
    (item) => item.kind === "text" && item.final !== false && item.text.trim().length > 0,
  );
}

/**
 * Returns true if the bubble at the given index could theoretically own an
 * outcome card (completed assistant, last with its responseId).
 *
 * Note: This is a pre-filter. The authoritative mapping from
 * `resolveTaskOutcomeAnchors` still determines whether a card actually renders.
 * This function is kept for backward compatibility with `BubbleView` logic but
 * is NOT sufficient on its own to determine card placement.
 */
export function ownsOutcomeCard(bubbles: readonly Bubble[], bubbleIndex: number): boolean {
  const bubble = bubbles[bubbleIndex];
  if (!bubble || bubble.kind !== "assistant" || bubble.lifecycle === "streaming") return false;
  // Not the last occurrence of this responseId
  return !bubbles.some(
    (candidate, index) =>
      index > bubbleIndex &&
      candidate.kind === "assistant" &&
      candidate.lifecycle !== "streaming" &&
      candidate.responseId === bubble.responseId,
  );
}

/**
 * Authoritative mapping from completed assistant bubble stableId to their
 * task run response IDs.
 *
 * Association rules:
 * 1. Exact match: if `task_run.response_id` exactly matches a completed
 *    assistant bubble's `responseId` AND that bubble has visible final text
 *    content, use that bubble's stableId.
 * 2. Triggering-message bridge: for native transcript ID mismatches (e.g.
 *    OpenCode-native), use `task_run.triggering_message_id` to find the
 *    user bubble that triggered the run, then attach only when exactly one
 *    completed assistant bubble has visible final text before the next user bubble.
 * 3. Fail closed: if neither exact match nor triggering_message_id gives an
 *    unambiguous association, no mapping is created for that run.
 * 4. Uniqueness: at most one outcome card per task run; at most one task run
 *    per assistant result bubble.
 * 5. Content requirement: ONLY bubbles with visible final text content qualify.
 *    Reasoning-only, tool-only, or empty bubbles do NOT own the task outcome.
 *
 * @param bubbles - The full bubble list in DOM order.
 * @param taskRuns - Persisted task run summaries for the session.
 * @returns Map of bubble stableId → task_run.response_id for bubbles
 *   that have an authoritative outcome association. No fallback to bubble.responseId.
 */
export function resolveTaskOutcomeAnchors(
  bubbles: readonly Bubble[],
  taskRuns: readonly TaskRunSummary[],
): ReadonlyMap<string, string> {
  // Map<bubbleStableId, taskRunResponseId>
  const result = new Map<string, string>();

  // Track which stableIds have already been assigned (uniqueness enforcement)
  const takenStableIds = new Set<string>();
  // Track which responseIds have been matched by exact match (for conflict detection)
  const exactMatchedResponseIds = new Set<string>();

  // Track runs that need triggering-message bridge resolution
  // Map<triggeringMessageId, taskRun>
  const triggeringBridgeCandidates = new Map<string, TaskRunSummary>();

  // First pass: exact matches by responseId
  // Only bubbles with VISIBLE FINAL TEXT content qualify.
  for (const run of taskRuns) {
    if (run.response_id == null) continue;
    if (run.terminal_status !== "completed") continue;

    // Find eligible bubbles with this exact responseId that have visible final text
    let eligibleBubble: Extract<Bubble, { kind: "assistant" }> | undefined;
    for (let index = bubbles.length - 1; index >= 0; index -= 1) {
      const candidate = bubbles[index];
      if (
        candidate?.kind === "assistant" &&
        candidate.responseId === run.response_id &&
        candidate.lifecycle !== "streaming" &&
        !takenStableIds.has(candidate.stableId) &&
        hasVisibleFinalText(candidate)
      ) {
        eligibleBubble = candidate;
        break;
      }
    }

    if (eligibleBubble) {
      // For exact matches where the bubble's responseId equals the task_run's response_id,
      // the mapped value equals the bubble's responseId (for API lookup compatibility).
      result.set(eligibleBubble.stableId, run.response_id);
      takenStableIds.add(eligibleBubble.stableId);
      exactMatchedResponseIds.add(run.response_id);
    } else if (run.triggering_message_id != null) {
      // No exact match eligible bubble found; record for triggering-message bridge.
      // Only store if we haven't already recorded a run for this triggering message.
      if (!triggeringBridgeCandidates.has(run.triggering_message_id)) {
        triggeringBridgeCandidates.set(run.triggering_message_id, run);
      }
    }
  }

  // Second pass: triggering-message bridge
  // For each user bubble whose itemId matches a triggering_message_id,
  // require exactly one completed assistant bubble with visible final text
  // before the next user bubble.
  for (let userIdx = 0; userIdx < bubbles.length; userIdx++) {
    const userBubble = bubbles[userIdx];
    if (userBubble.kind !== "user") continue;

    const triggeringRun = triggeringBridgeCandidates.get(userBubble.itemId);
    if (!triggeringRun || triggeringRun.response_id == null) continue;

    const candidates: Array<Extract<Bubble, { kind: "assistant" }>> = [];
    for (let i = userIdx + 1; i < bubbles.length; i++) {
      const candidate = bubbles[i]!;
      if (candidate.kind === "user") break;
      if (
        candidate.kind === "assistant" &&
        candidate.lifecycle !== "streaming" &&
        !takenStableIds.has(candidate.stableId) &&
        hasVisibleFinalText(candidate)
      ) {
        candidates.push(candidate);
      }
    }

    if (candidates.length === 1) {
      const targetBubble = candidates[0]!;
      result.set(targetBubble.stableId, triggeringRun.response_id);
      takenStableIds.add(targetBubble.stableId);
    }
  }

  return result;
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
  /** Legacy execution projection; evaluator activity never changes this. */
  terminal_status: "running" | "completed" | "failed" | "cancelled" | "incomplete";
  execution_status?:
    | "queued"
    | "starting"
    | "running"
    | "cancelling"
    | "cancelled"
    | "completed"
    | "failed"
    | "timed_out";
  evaluation_status?: "not_requested" | "pending" | "completed" | "skipped" | "failed";
  execution_started_at?: number | null;
  execution_finished_at?: number | null;
  execution_duration_ms?: number | null;
  evaluation_started_at?: number | null;
  evaluation_finished_at?: number | null;
  timeout_type?: string | null;
  last_useful_activity_at?: number | null;
  actual_provider?: string | null;
  actual_provider_model?: string | null;
  actual_provenance_verified?: boolean | null;
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
  /**
   * The transcript message id that triggered this task run.
   * Used as a stable bridge when the assistant bubble's responseId
   * differs from the task_run's response_id (e.g. OpenCode-native
   * transcript IDs vs Omnigent execution response IDs).
   */
  triggering_message_id: string | null;
  terminal_status: TaskRun["terminal_status"];
  started_at: number | null;
  terminal_at: number | null;
  duration_ms: number | null;
  selected_provider: string | null;
  selected_model: string | null;
  requested_route_id: string | null;
  routing_proposal_id?: string | null;
  routing_decision_id?: string | null;
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

export interface TaskRunRoutingContext {
  proposal_id: string;
  decision_id: string;
  proposed: {
    harness: string | null;
    provider: string | null;
    model: string | null;
    route_id: string | null;
    reasoning_effort: string | null;
    permission_mode: string | null;
  };
  approved: TaskRunRoutingContext["proposed"] & {
    action: "approved" | "changed" | "declined";
  };
}

export interface TaskRunSelectionContext {
  source:
    | "routing_agent"
    | "user_selected_model"
    | "user_selected_route"
    | "session_default"
    | "unknown";
  requested: {
    harness: string | null;
    provider: string | null;
    model: string | null;
    route_id: string | null;
    reasoning_effort: string | null;
    permission_mode: string | null;
  };
}

/** Aggregate response of `GET /v1/task-runs/{id}`. */
export interface TaskRunDetailResponse {
  run: TaskRun;
  routing?: TaskRunRoutingContext | null;
  selection: TaskRunSelectionContext;
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
  signal?: AbortSignal,
): Promise<ListSessionTaskRunsResponse> {
  const url = `/v1/sessions/${encodeURIComponent(sessionId)}/task-runs?limit=${limit}`;
  const resp = await authenticatedFetch(url, {
    credentials: "same-origin",
    ...(signal ? { signal } : {}),
  });
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
  const detail: TaskRunDetailResponse = await resp.json();
  if (detail.run.response_id !== responseId) {
    throw new TaskRunResponseIdentityError(responseId, detail.run.response_id);
  }
  return detail;
}

/**
 * Authoritative, session-scoped eligibility for inline outcome cards. The
 * registry comes only from persisted task runs; transcript order and bubble
 * content are deliberately never used to infer task ownership.
 *
 * Returns the complete persisted task run summaries, which callers can use
 * with `resolveTaskOutcomeAnchors` to get the bubble-to-task-run mapping.
 */
export function useTaskOutcomeRuns(
  sessionId: string | null | undefined,
  streamStatus: "idle" | "streaming",
  bubbles: readonly Bubble[] = [],
): readonly TaskRunSummary[] {
  const [registry, setRegistry] = useState<{
    sessionId: string | null;
    runs: readonly TaskRunSummary[];
  }>({ sessionId: null, runs: [] });
  const statusRef = useRef(streamStatus);
  const statusSessionRef = useRef(sessionId ?? null);
  const registryRef = useRef(registry);
  const bubblesRef = useRef(bubbles);
  registryRef.current = registry;
  bubblesRef.current = bubbles;

  const updateRegistry = (currentSessionId: string, incoming: readonly TaskRunSummary[]) => {
    const previous = registryRef.current;
    if (previous.sessionId !== currentSessionId) return;
    const byId = new Map(previous.runs.map((run) => [run.id, run]));
    for (const run of incoming) byId.set(run.id, run);
    const next = { sessionId: currentSessionId, runs: [...byId.values()] };
    registryRef.current = next;
    setRegistry(next);
  };

  useEffect(() => {
    const controller = new AbortController();
    if (!sessionId) {
      registryRef.current = { sessionId: null, runs: [] };
      setRegistry({ sessionId: null, runs: [] });
      return () => controller.abort();
    }
    // Clear synchronously so a previous session can never qualify the new one.
    const empty = { sessionId, runs: [] as readonly TaskRunSummary[] };
    registryRef.current = empty;
    setRegistry(empty);
    void listSessionTaskRuns(sessionId, 200, controller.signal)
      .then(({ runs }) => {
        if (!controller.signal.aborted && registryRef.current.sessionId === sessionId) {
          updateRegistry(sessionId, runs);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) console.warn("Failed to load task outcome registry", error);
      });
    return () => controller.abort();
  }, [sessionId]);

  useEffect(() => {
    const sessionChanged = statusSessionRef.current !== (sessionId ?? null);
    statusSessionRef.current = sessionId ?? null;
    const wasStreaming = statusRef.current === "streaming";
    statusRef.current = streamStatus;
    if (sessionChanged || !sessionId || !wasStreaming || streamStatus !== "idle") return;

    const controller = new AbortController();
    // Snapshot the registry before completion. A run is a discovery result only
    // when its persisted ID was not present at the terminal boundary.
    const baselineIds = new Set(registryRef.current.runs.map((run) => run.id));
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delayIndex = -1; // -1 is the immediate request; then 800..6400ms.

    const discover = async (): Promise<void> => {
      if (controller.signal.aborted) return;
      try {
        const { runs } = await listSessionTaskRuns(sessionId, 200, controller.signal);
        if (controller.signal.aborted) return;
        updateRegistry(sessionId, runs);
        const merged = registryRef.current.runs;
        const discovered = merged.some(
          (run) =>
            !baselineIds.has(run.id) &&
            resolveTaskOutcomeAnchors(bubblesRef.current, [run]).size > 0,
        );
        if (discovered) return;
        delayIndex += 1;
        if (delayIndex >= TASK_RUN_DISCOVERY_DELAYS_MS.length) return;
        timer = setTimeout(() => void discover(), TASK_RUN_DISCOVERY_DELAYS_MS[delayIndex]);
      } catch (error) {
        if (!controller.signal.aborted) {
          console.warn("Failed to discover task outcome registry", error);
          delayIndex += 1;
          if (delayIndex < TASK_RUN_DISCOVERY_DELAYS_MS.length) {
            timer = setTimeout(() => void discover(), TASK_RUN_DISCOVERY_DELAYS_MS[delayIndex]);
          }
        }
      }
    };
    void discover();
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [sessionId, streamStatus]);

  return registry.sessionId === sessionId ? registry.runs : EMPTY_TASK_OUTCOME_RUNS;
}

const EMPTY_TASK_OUTCOME_RUNS: readonly TaskRunSummary[] = [];

/**
 * Legacy compatibility hook: returns the set of bubble responseIds that have
 * a task run. Prefer `useTaskOutcomeRuns` with `resolveTaskOutcomeAnchors` for
 * proper OpenCode-native support.
 *
 * @deprecated Use `useTaskOutcomeRuns` instead for accurate bubble-to-task-run mapping.
 */
export function useTaskOutcomeResponseIds(
  sessionId: string | null | undefined,
  streamStatus: "idle" | "streaming",
): ReadonlySet<string> {
  const runs = useTaskOutcomeRuns(sessionId, streamStatus);
  // For backward compatibility: return the bubble responseIds that exactly match task run response_ids.
  // This is the same behavior as before, which works for non-OpenCode-native harnesses.
  return useMemo(
    () => new Set(runs.flatMap((run) => (run.response_id ? [run.response_id] : []))),
    [runs],
  );
}

/**
 * Memoized helper to derive bubble responseId → task_run.response_id mapping.
 * Call this with bubbles from React state and runs from `useTaskOutcomeRuns`.
 *
 * @param bubbles - Current bubble list from chat store.
 * @param runs - Task run summaries from `useTaskOutcomeRuns`.
 * @returns Map from bubble responseId to the task_run.response_id to use for API calls.
 */
export function useTaskOutcomeAnchors(
  bubbles: readonly Bubble[],
  runs: readonly TaskRunSummary[],
): ReadonlyMap<string, string> {
  return useMemo(() => resolveTaskOutcomeAnchors(bubbles, runs), [bubbles, runs]);
}

export async function getTaskRun(taskRunId: string): Promise<TaskRunDetailResponse> {
  const url = `/v1/task-runs/${encodeURIComponent(taskRunId)}`;
  const resp = await authenticatedFetch(url, { credentials: "same-origin" });
  if (!resp.ok) {
    throw new TaskRunFetchError(resp.status, `getTaskRun failed: ${resp.status}`);
  }
  return resp.json();
}

export async function reEvaluateTaskRun(
  taskRunId: string,
): Promise<{ status: "queued" | "already_present" | string }> {
  const url = `/v1/task-runs/${encodeURIComponent(taskRunId)}/evaluate`;
  const resp = await authenticatedFetch(url, {
    method: "POST",
    credentials: "same-origin",
  });
  if (!resp.ok && resp.status !== 409) {
    const text = await resp.text().catch(() => "");
    throw new TaskRunFetchError(
      resp.status,
      `reEvaluateTaskRun failed: ${resp.status} ${text.slice(0, 200)}`,
    );
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
  /** A server identity violation is terminal and must not render a card. */
  identityMismatch: boolean;
  /** Restart the bounded polling cycle (also used by the [Retry] button). */
  retry: () => void;
}
