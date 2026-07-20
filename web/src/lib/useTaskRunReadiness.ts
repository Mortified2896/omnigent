// Bounded readiness polling for the inline TaskOutcomeBriefCard.
//
// The recorder and evaluator index task runs asynchronously after the
// final assistant response streams; the inline card must therefore
// refetch a few times before giving up. This module is intentionally
// separate from `./taskOutcomes` so vitest's ``vi.mock("@/lib/taskOutcomes", ...)``
// fully replaces the API surface and the hook here always calls the
// (possibly mocked) fetcher rather than a sibling reference inside the
// same module.

import { useCallback, useEffect, useState } from "react";
import {
  getTaskRunForResponse,
  TASK_RUN_READINESS_DELAYS_MS,
  TASK_RUN_READINESS_MAX_ATTEMPTS,
  TaskRunFetchError,
  TaskRunResponseIdentityError,
} from "./taskOutcomes";
import type {
  TaskRunDetailResponse,
  TaskRunReadiness,
  TaskRunReadinessPhase,
} from "./taskOutcomes";

function isRetryableStatus(status: number): boolean {
  // 404 is the by-response timing race we explicitly want to absorb.
  // 5xx and network errors are NOT retried silently — the operator
  // should see them via the "failed" phase.
  return status === 404;
}

/**
 * Bounded readiness polling for ``GET .../by-response/{response_id}``.
 *
 * Guarantees:
 *
 * - exactly one in-flight fetch per (sessionId, responseId) generation;
 * - stale fetches (key change, unmount) never overwrite newer state;
 * - bounded exponential backoff ({@link TASK_RUN_READINESS_DELAYS_MS});
 * - a single retry budget shared by 404 and evaluation-null paths;
 * - all timers / abort signals torn down on unmount or key change.
 */
export function useTaskRunForResponse(sessionId: string, responseId: string): TaskRunReadiness {
  const [phase, setPhase] = useState<TaskRunReadinessPhase>("loading");
  const [detail, setDetail] = useState<TaskRunDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [identityMismatch, setIdentityMismatch] = useState(false);
  // Generation token: bumped on every retry or key change so an in-flight
  // fetch can detect it has been superseded.
  const [generation, setGeneration] = useState(0);
  const retry = useCallback(() => {
    setDetail(null);
    setError(null);
    setIdentityMismatch(false);
    setPhase("loading");
    setGeneration((g) => g + 1);
  }, []);

  useEffect(() => {
    // Reset on every key change. The retry budget is reset by the
    // effect re-running (since ``generation`` is in the dep list).
    setDetail(null);
    setError(null);
    setIdentityMismatch(false);
    setPhase("loading");
    const localGeneration = generation;

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Counter local to this effect instance — survives across the
    // recursive setTimeout chain.
    let attempt = 0;

    const scheduleNext = (delay: number) => {
      if (controller.signal.aborted) return;
      timer = setTimeout(() => {
        void run();
      }, delay);
    };

    const run = async (): Promise<void> => {
      if (controller.signal.aborted) return;
      try {
        const next = await getTaskRunForResponse(sessionId, responseId, controller.signal);
        if (controller.signal.aborted) return;
        if (generation !== localGeneration) return;
        setDetail(next);
        setError(null);
        // Human review present → final state, stop polling.
        if (next.review) {
          setPhase("ready");
          return;
        }
        // Evaluation present → ready. Accept/Adjust/Decline are now meaningful.
        if (next.evaluation) {
          setPhase("ready");
          return;
        }
        // Explicit durable terminal evaluator states render immediately even
        // when deferred/failed intentionally have no evaluation row.
        const evaluationStatus = next.run.evaluation_status ?? "pending";
        if (["deferred", "failed", "skipped"].includes(evaluationStatus)) {
          setPhase("ready");
          return;
        }
        if (evaluationStatus === "completed") {
          setError("Evaluation lifecycle invariant violated: completed without a judgment row.");
          setPhase("failed");
          return;
        }
        // Pending/not-requested rows are retried until the UI budget is gone.
        attempt += 1;
        if (attempt > TASK_RUN_READINESS_MAX_ATTEMPTS) {
          setPhase("exhausted");
          return;
        }
        setPhase("waiting");
        const delay =
          TASK_RUN_READINESS_DELAYS_MS[attempt - 1] ??
          TASK_RUN_READINESS_DELAYS_MS[TASK_RUN_READINESS_DELAYS_MS.length - 1];
        scheduleNext(delay);
      } catch (e) {
        if (controller.signal.aborted) return;
        if (generation !== localGeneration) return;
        if (e instanceof TaskRunFetchError && isRetryableStatus(e.status)) {
          attempt += 1;
          if (attempt > TASK_RUN_READINESS_MAX_ATTEMPTS) {
            setDetail(null);
            setPhase("exhausted");
            return;
          }
          setPhase("waiting");
          const delay =
            TASK_RUN_READINESS_DELAYS_MS[attempt - 1] ??
            TASK_RUN_READINESS_DELAYS_MS[TASK_RUN_READINESS_DELAYS_MS.length - 1];
          scheduleNext(delay);
          return;
        }
        // A mismatched response identity is a server invariant violation,
        // not an unavailable evaluation. Never attach or retry that run.
        if (e instanceof TaskRunResponseIdentityError) {
          setDetail(null);
          setIdentityMismatch(true);
          setPhase("failed");
          return;
        }
        // Non-retryable error. Keep `detail` empty so we don't pretend
        // the task run was found.
        setDetail(null);
        setError(e instanceof Error ? e.message : String(e));
        setPhase("failed");
      }
    };

    void run();
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [sessionId, responseId, generation]);

  return { phase, detail, error, identityMismatch, retry };
}
