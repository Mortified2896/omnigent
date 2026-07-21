import { useEffect, useState } from "react";
import { CheckIcon, LoaderIcon, PencilIcon, RefreshCwIcon, XIcon } from "lucide-react";
import { reEvaluateTaskRun, submitTaskRunReview } from "@/lib/taskOutcomes";
import type { TaskRunDetailResponse } from "@/lib/taskOutcomes";
import { useTaskRunForResponse } from "@/lib/useTaskRunReadiness";
import { TaskReviewCard } from "./TaskReviewCard";

/** Storage key prefix for the per-session, per-run local dismissal marker. */
const POSTPONED_STORAGE_PREFIX = "omnigent:outcome:postponed:";

/**
 * Read a local "postponed" marker for a (session, taskRun) pair from
 * sessionStorage. Returns ``true`` when the user previously clicked
 * "Review later" for this exact run in this exact session.
 *
 * sessionStorage may be unavailable (Safari private mode, sandboxed
 * iframes, tests with a stubbed window). Every failure is swallowed:
 * the dismissal is a UI hint, not a state-of-record, so a missing /
 * unavailable store simply renders the full card again.
 */
function readPostponed(sessionId: string, taskRunId: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return (
      window.sessionStorage.getItem(POSTPONED_STORAGE_PREFIX + sessionId + ":" + taskRunId) !== null
    );
  } catch {
    return false;
  }
}

/**
 * Mirror :func:`readPostponed` for the write side. Tolerates a missing
 * or throwing ``window.sessionStorage`` — the marker is a usability
 * hint, not authoritative state.
 */
function writePostponed(sessionId: string, taskRunId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(POSTPONED_STORAGE_PREFIX + sessionId + ":" + taskRunId, "1");
  } catch {
    // Ignore quota / availability errors — the card still collapses
    // locally for the current viewing context.
  }
}

function clearPostponed(sessionId: string, taskRunId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(POSTPONED_STORAGE_PREFIX + sessionId + ":" + taskRunId);
  } catch {
    // See writePostponed.
  }
}

/** Compact, response-keyed approval surface for completed harness tasks. */
export function TaskOutcomeBriefCard({
  sessionId,
  responseId,
}: {
  sessionId: string;
  responseId: string;
}) {
  const { phase, detail, error, identityMismatch, retry } = useTaskRunForResponse(
    sessionId,
    responseId,
  );
  const [editing, setEditing] = useState(false);
  const [mutating, setMutating] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [reEvaluating, setReEvaluating] = useState(false);
  const [reEvaluateError, setReEvaluateError] = useState<string | null>(null);
  // sessionStorage-scoped dismissal. The task remains in the
  // unreviewed-outcomes queue — this only hides the inline card
  // for the current tab until the tab is closed.
  const [postponed, setPostponed] = useState(false);
  const [postponedPersisted, setPostponedPersisted] = useState(false);

  // Once we know the run id, hydrate the optional sessionStorage
  // dismissal marker. Done as an effect (not at render time) so SSR /
  // tests don't touch window during render.
  useEffect(() => {
    if (!detail?.run?.id) return;
    if (readPostponed(sessionId, detail.run.id)) {
      setPostponed(true);
      setPostponedPersisted(true);
    }
  }, [detail?.run?.id, sessionId]);

  const postponeRun = () => {
    if (!detail?.run?.id) return;
    writePostponed(sessionId, detail.run.id);
    setPostponedPersisted(true);
    setPostponed(true);
    setEditing(false);
  };

  const reEvaluate = async () => {
    if (reEvaluating) return;
    const taskRunId = detail?.run?.id;
    if (!taskRunId) {
      setReEvaluateError("Task run is not available for re-evaluation.");
      return;
    }
    setReEvaluating(true);
    setReEvaluateError(null);
    try {
      await reEvaluateTaskRun(taskRunId);
      retry();
    } catch (cause) {
      setReEvaluateError(
        cause instanceof Error ? cause.message : "Could not queue the outcome evaluation",
      );
    } finally {
      setReEvaluating(false);
    }
  };

  // The registry selected this response, but never trust an endpoint that
  // violates the same identity. Rendering nothing prevents misattachment.
  if (identityMismatch) return null;

  if (phase === "loading" || phase === "waiting") {
    const evaluationPending =
      phase === "loading" || Boolean(detail?.run && detail.evaluation === null);
    return (
      <div className="mt-2 text-xs text-muted-foreground" data-testid="outcome-brief-pending">
        <LoaderIcon className="mr-1 inline size-3 animate-spin" />
        {evaluationPending
          ? `Evaluating outcome via OmniRoute Outcome Scoring… Preferred evaluator: MiniMax-M3${detail?.run.evaluation_attempt_count ? ` · Attempt ${detail.run.evaluation_attempt_count}` : ""}`
          : "Preparing outcome brief…"}
      </div>
    );
  }

  if (phase === "failed") {
    return (
      <div
        className="mt-2 flex items-center gap-2 text-xs text-muted-foreground"
        data-testid="outcome-brief-failed"
      >
        <span>Outcome brief unavailable</span>
        <button
          type="button"
          className="ml-1 underline"
          onClick={retry}
          data-testid="outcome-brief-retry"
        >
          <RefreshCwIcon className="mr-1 inline size-3" />
          Retry
        </button>
        {error ? <span className="text-rose-600">({error})</span> : null}
      </div>
    );
  }

  if (phase === "exhausted") {
    // The task run still hasn't shown up after the retry budget —
    // there is nothing for the operator to act on, so render a
    // compact retry surface and leave the unreviewed-outcomes queue
    // to surface the run later.
    if (!detail?.run) {
      return (
        <div
          className="mt-2 flex items-center gap-2 text-xs text-muted-foreground"
          data-testid="outcome-brief-exhausted"
        >
          <span>Outcome brief not ready yet</span>
          <button
            type="button"
            className="ml-1 underline"
            onClick={retry}
            data-testid="outcome-brief-retry"
          >
            <RefreshCwIcon className="mr-1 inline size-3" />
            Retry
          </button>
        </div>
      );
    }
    if (editing) {
      return (
        <TaskReviewCard
          taskRunId={detail.run.id}
          initialRun={detail.run}
          isOpen
          onOpenChange={() => setEditing(false)}
          onReviewed={() => {
            setEditing(false);
            retry();
          }}
          onReviewLater={postponeRun}
        />
      );
    }
    const restoreCard = () => {
      if (postponedPersisted) {
        clearPostponed(sessionId, detail.run!.id);
        setPostponedPersisted(false);
      }
      setPostponed(false);
    };
    if (postponed) {
      return (
        <div
          className="mt-2 flex items-center gap-2 rounded-md border px-3 py-2 text-xs"
          data-testid="outcome-brief-postponed"
          data-task-run-id={detail.run.id}
        >
          <span>Outcome review postponed</span>
          <button className="ml-auto underline" onClick={restoreCard}>
            Review now
          </button>
        </div>
      );
    }
    // Run exists but the evaluator still hasn't produced a row.
    // We deliberately don't render a fabricated "unsure" verdict —
    // Adjust can still submit a human review without an evaluation.
    return (
      <div
        className="mt-2 flex items-center gap-2 rounded-md border px-3 py-2 text-xs"
        data-testid="outcome-brief-evaluation-unavailable"
        data-task-run-id={detail.run.id}
      >
        <span>Outcome evaluation unavailable</span>
        <button className="ml-auto underline" onClick={() => setEditing(true)}>
          <PencilIcon className="mr-1 inline size-3" />
          Adjust
        </button>
        <button
          className="underline disabled:opacity-50"
          onClick={() => void reEvaluate()}
          disabled={reEvaluating}
          data-testid="outcome-brief-retry"
        >
          {reEvaluating ? (
            <LoaderIcon className="mr-1 inline size-3 animate-spin" />
          ) : (
            <RefreshCwIcon className="mr-1 inline size-3" />
          )}
          {reEvaluating ? "Re-evaluating…" : "Retry"}
        </button>
        {reEvaluateError ? (
          <span role="alert" className="text-destructive">
            {reEvaluateError}
          </span>
        ) : null}
        <button
          className="rounded border px-2 py-1"
          data-testid="outcome-review-later"
          onClick={() => {
            writePostponed(sessionId, detail.run!.id);
            setPostponedPersisted(true);
            setPostponed(true);
          }}
        >
          Review later
        </button>
      </div>
    );
  }

  // phase === "ready" — detail contains either an evaluation, review, or an
  // explicit durable deferred/failed/skipped lifecycle.
  if (!detail) return null;
  if (!detail.review && !detail.evaluation && detail.run.evaluation_status === "deferred") {
    return renderEvaluationIssueCard({
      detail,
      kind: "deferred",
      editing,
      setEditing,
      sessionId,
      postponed,
      postponedPersisted,
      setPostponed,
      setPostponedPersisted,
      retry,
      reEvaluating,
      reEvaluate,
      reEvaluateError,
    });
  }
  if (!detail.review && !detail.evaluation && detail.run.evaluation_status === "failed") {
    return renderEvaluationIssueCard({
      detail,
      kind: "failed",
      editing,
      setEditing,
      sessionId,
      postponed,
      postponedPersisted,
      setPostponed,
      setPostponedPersisted,
      retry,
      reEvaluating,
      reEvaluate,
      reEvaluateError,
    });
  }
  return renderReadyCard({
    detail,
    editing,
    setEditing,
    sessionId,
    postponed,
    postponedPersisted,
    setPostponed,
    setPostponedPersisted,
    onReviewed: retry,
    onReviewLater: postponeRun,
    mutating,
    setMutating,
    mutationError,
    setMutationError,
  });
}

function formatTimestamp(epoch: number | null | undefined): string {
  if (epoch == null) return "Not scheduled";
  return new Date(epoch * 1000).toLocaleString();
}

function renderEvaluationIssueCard(args: {
  detail: TaskRunDetailResponse;
  kind: "deferred" | "failed";
  editing: boolean;
  setEditing: (value: boolean) => void;
  sessionId: string;
  postponed: boolean;
  postponedPersisted: boolean;
  setPostponed: (value: boolean) => void;
  setPostponedPersisted: (value: boolean) => void;
  retry: () => void;
  reEvaluating: boolean;
  reEvaluate: () => Promise<void>;
  reEvaluateError: string | null;
}) {
  const {
    detail,
    kind,
    editing,
    setEditing,
    sessionId,
    postponed,
    postponedPersisted,
    setPostponed,
    setPostponedPersisted,
    retry,
    reEvaluating,
    reEvaluate,
    reEvaluateError,
  } = args;
  const { run } = detail;
  const postpone = () => {
    writePostponed(sessionId, run.id);
    setPostponedPersisted(true);
    setPostponed(true);
  };
  if (editing) {
    return (
      <TaskReviewCard
        taskRunId={run.id}
        initialRun={run}
        isOpen
        onOpenChange={() => setEditing(false)}
        onReviewed={() => {
          setEditing(false);
          retry();
        }}
        onReviewLater={postpone}
      />
    );
  }
  if (postponed) {
    return (
      <div
        className="mt-2 flex items-center gap-2 rounded-md border px-3 py-2 text-xs"
        data-testid="outcome-brief-postponed"
        data-task-run-id={run.id}
      >
        <span>Outcome review postponed</span>
        <button
          className="ml-auto underline"
          onClick={() => {
            if (postponedPersisted) clearPostponed(sessionId, run.id);
            setPostponedPersisted(false);
            setPostponed(false);
          }}
        >
          Review now
        </button>
      </div>
    );
  }
  const deferred = kind === "deferred";
  return (
    <div
      className={`mt-2 rounded-md border px-3 py-3 text-xs shadow-sm ${deferred ? "border-amber-500/60 bg-amber-500/5" : "border-destructive bg-destructive/5"}`}
      data-testid={deferred ? "outcome-brief-deferred" : "outcome-brief-evaluator-failed"}
      data-task-run-id={run.id}
    >
      <div className="font-semibold">
        {deferred ? "Outcome evaluation deferred" : "Outcome evaluator requires attention"}
      </div>
      <p className="mt-1">
        {deferred
          ? "MiniMax-M3 is currently unavailable on both evaluator accounts. No scoring model is currently available."
          : `Category: ${run.evaluation_error_kind ?? "operator attention"}.`}
      </p>
      <p className="mt-1 text-muted-foreground">
        Preferred evaluator: MiniMax-M3. The retry will reuse the same route.
      </p>
      <dl className="mt-2 grid gap-1 text-muted-foreground">
        <div>
          Requested evaluator route: {run.evaluation_requested_model ?? "custom/outcome-scoring"}
        </div>
        <div>Attempts: {run.evaluation_attempt_count ?? 0}</div>
        <div>Last attempt: {formatTimestamp(run.evaluation_last_attempt_at)}</div>
        {deferred && run.evaluation_next_retry_at != null ? (
          <div>Next automatic retry: {formatTimestamp(run.evaluation_next_retry_at)}</div>
        ) : null}
        <div>
          Reason:{" "}
          {run.evaluation_error_message ?? run.evaluation_error_code ?? "Unknown evaluator error"}
        </div>
      </dl>
      {reEvaluateError ? (
        <p role="alert" className="mt-2 text-destructive">
          {reEvaluateError}
        </p>
      ) : null}
      <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
        <button
          className="min-h-11 rounded bg-primary px-2 py-1 text-primary-foreground disabled:opacity-50"
          onClick={() => void reEvaluate()}
          disabled={reEvaluating}
          data-testid="outcome-brief-retry"
        >
          {reEvaluating ? (
            <LoaderIcon className="mr-1 inline size-3 animate-spin" />
          ) : (
            <RefreshCwIcon className="mr-1 inline size-3" />
          )}
          Retry now
        </button>
        <button className="min-h-11 rounded border px-2 py-1" onClick={() => setEditing(true)}>
          <PencilIcon className="mr-1 inline size-3" />
          {deferred ? "Adjust / Review manually" : "Review manually"}
        </button>
        <button
          className="min-h-11 underline sm:ml-auto"
          data-testid="outcome-review-later"
          onClick={postpone}
        >
          Review later
        </button>
      </div>
    </div>
  );
}

function renderReadyCard(args: {
  detail: TaskRunDetailResponse;
  editing: boolean;
  setEditing: (v: boolean) => void;
  sessionId: string;
  postponed: boolean;
  postponedPersisted: boolean;
  setPostponed: (v: boolean) => void;
  setPostponedPersisted: (v: boolean) => void;
  onReviewed: () => void;
  onReviewLater: () => void;
  mutating: boolean;
  setMutating: (value: boolean) => void;
  mutationError: string | null;
  setMutationError: (value: string | null) => void;
}) {
  const {
    detail,
    editing,
    setEditing,
    sessionId,
    postponed,
    postponedPersisted,
    setPostponed,
    setPostponedPersisted,
    onReviewed,
    onReviewLater,
    mutating,
    setMutating,
    mutationError,
    setMutationError,
  } = args;
  const { run, evaluation, review } = detail;
  const hasEvaluation = evaluation !== null;

  if (review && !editing) {
    return (
      <div
        className="mt-2 flex items-center gap-2 rounded-md border px-3 py-2 text-xs"
        data-testid="outcome-brief-status"
      >
        <span>
          {review.review_action === "declined" || review.review_action === "not_logged"
            ? "Excluded from routing learning"
            : `Outcome ${review.review_action === "adjusted" ? "adjusted" : "accepted"} · ${review.verdict}`}
        </span>
        <button className="ml-auto underline" onClick={() => setEditing(true)}>
          <PencilIcon className="mr-1 inline size-3" />
          Edit
        </button>
      </div>
    );
  }
  if (editing) {
    return (
      <TaskReviewCard
        taskRunId={run.id}
        initialRun={run}
        isOpen
        onOpenChange={() => setEditing(false)}
        onReviewed={() => {
          setEditing(false);
          onReviewed();
        }}
        onReviewLater={onReviewLater}
      />
    );
  }
  const verdict = evaluation?.verdict;
  const family = evaluation?.proposed_task_family ?? "—";
  const confidence =
    evaluation?.confidence == null ? "—" : `${Math.round(evaluation.confidence * 100)}%`;
  const evidence = run.changed_files?.length ? `${run.changed_files.length} files changed` : "—";
  const act = async (action: "accept" | "adjust" | "decline" | "dont_log") => {
    if (mutating || (action === "accept" && !evaluation)) return;
    setMutating(true);
    setMutationError(null);
    try {
      await submitTaskRunReview(run.id, {
        action,
        source_evaluation_id: action === "accept" ? evaluation?.id : undefined,
        verdict: action === "decline" || action === "dont_log" ? "skipped" : undefined,
      });
      onReviewed();
    } catch (cause) {
      setMutating(false);
      setMutationError(cause instanceof Error ? cause.message : "Could not save the review");
    }
  };
  const postponeCard = () => {
    // Mark locally only — no review POST is issued, and the task
    // continues to appear in the unreviewed-outcomes queue.
    writePostponed(sessionId, run.id);
    setPostponedPersisted(true);
    setPostponed(true);
  };
  const restoreCard = () => {
    if (postponedPersisted) {
      clearPostponed(sessionId, run.id);
      setPostponedPersisted(false);
    }
    setPostponed(false);
  };
  if (postponed) {
    return (
      <div
        className="mt-2 flex items-center gap-2 rounded-md border px-3 py-2 text-xs"
        data-testid="outcome-brief-postponed"
        data-task-run-id={run.id}
      >
        <span>Outcome review postponed</span>
        <button className="ml-auto underline" onClick={restoreCard}>
          Review now
        </button>
      </div>
    );
  }
  return (
    <div
      className="mt-2 rounded-md border bg-card px-3 py-2 text-xs shadow-sm"
      data-testid="task-outcome-brief-card"
      data-task-run-id={run.id}
    >
      <div className="font-medium">Task outcome</div>
      <div className="mt-1 text-sm">
        {verdict} · {family} · Quality {evaluation?.quality_score ?? "—"}/5
      </div>
      <div className="mt-1 text-muted-foreground">
        Requested combo: {run.requested_route_id ?? "—"} · Requested reasoning:{" "}
        {run.reasoning_effort ?? "—"}
      </div>
      <div className="mt-1 text-muted-foreground">
        Executed provider/model:{" "}
        {run.actual_provider && run.actual_provider_model
          ? `${run.actual_provider}/${run.actual_provider_model}`
          : "Unavailable"}{" "}
        · Execution provenance: {run.actual_provenance_verified ? "Verified" : "Unverified"}
        {" · "}Fallback: {run.fallback_used == null ? "—" : run.fallback_used ? "yes" : "no"}
      </div>
      <div className="mt-1 text-muted-foreground">
        Evidence: ✓ {evidence} · Commit {run.commit_sha?.slice(0, 8) ?? "—"} · Evaluator confidence:{" "}
        {confidence}
      </div>
      <div className="mt-1 text-muted-foreground">
        Evaluator: {evaluation?.evaluator_provider ?? "—"} / {evaluation?.evaluator_model ?? "—"}
        {" · "}Fallback: {evaluation?.evaluator_fallback_used ? "yes" : "no"}
        {evaluation?.evaluator_decision_id ? ` · Decision ${evaluation.evaluator_decision_id}` : ""}
      </div>
      {evaluation?.verdict === "inconclusive" && evaluation.reasoning ? (
        <details className="mt-2 text-muted-foreground" data-testid="inconclusive-reasoning">
          <summary className="cursor-pointer">Why the evaluation was inconclusive</summary>
          <p className="mt-1 whitespace-pre-wrap">{evaluation.reasoning}</p>
        </details>
      ) : null}
      {mutationError && (
        <p role="alert" className="mt-2 text-destructive">
          {mutationError}
        </p>
      )}
      <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:flex-wrap" aria-busy={mutating}>
        <button
          className="min-h-11 w-full rounded bg-primary px-2 py-1 text-primary-foreground disabled:opacity-50 sm:w-auto"
          onClick={() => void act("accept")}
          disabled={!hasEvaluation || mutating}
          title={
            hasEvaluation ? undefined : "Accept requires an automated evaluation to be present."
          }
        >
          <CheckIcon className="mr-1 inline size-3" />
          Accept
        </button>
        <button
          className="min-h-11 w-full rounded border px-2 py-1 sm:w-auto"
          disabled={mutating}
          onClick={() => setEditing(true)}
        >
          <PencilIcon className="mr-1 inline size-3" />
          Adjust
        </button>
        <button
          className="min-h-11 w-full rounded border px-2 py-1 sm:w-auto"
          disabled={mutating}
          onClick={() => void act("dont_log")}
        >
          <XIcon className="mr-1 inline size-3" />
          Don’t log
        </button>
        <button
          className="min-h-11 w-full sm:ml-auto sm:w-auto underline"
          data-testid="outcome-review-later"
          disabled={mutating}
          onClick={postponeCard}
        >
          Review later
        </button>
      </div>
    </div>
  );
}
