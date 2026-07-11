import { useEffect, useState } from "react";
import { CheckIcon, LoaderIcon, PencilIcon, RefreshCwIcon, XIcon } from "lucide-react";
import { submitTaskRunReview } from "@/lib/taskOutcomes";
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

  // The registry selected this response, but never trust an endpoint that
  // violates the same identity. Rendering nothing prevents misattachment.
  if (identityMismatch) return null;

  if (phase === "loading" || phase === "waiting") {
    return (
      <div className="mt-2 text-xs text-muted-foreground" data-testid="outcome-brief-pending">
        <LoaderIcon className="mr-1 inline size-3 animate-spin" />
        Preparing outcome brief…
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
        <button className="underline" onClick={retry} data-testid="outcome-brief-retry">
          <RefreshCwIcon className="mr-1 inline size-3" />
          Retry
        </button>
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

  // phase === "ready" — detail must exist and have either an evaluation
  // or a human review. Defensive guard kept for clarity.
  if (!detail) return null;
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
  });
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
          {review.review_action === "declined"
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
      />
    );
  }
  const verdict = evaluation?.verdict ?? "unsure";
  const family = evaluation?.proposed_task_family ?? run.proposed_task_family ?? "—";
  const confidence =
    evaluation?.confidence == null ? "—" : `${Math.round(evaluation.confidence * 100)}%`;
  const evidence = run.changed_files?.length ? `${run.changed_files.length} files changed` : "—";
  const act = async (action: "accept" | "adjust" | "decline") => {
    if (action === "accept" && !evaluation) return;
    await submitTaskRunReview(run.id, {
      action,
      source_evaluation_id: action === "accept" ? evaluation?.id : undefined,
      verdict: action === "decline" ? "skipped" : undefined,
    });
    onReviewed();
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
        Likely {verdict} · {family} · Quality {evaluation?.quality_score ?? "—"}/5
      </div>
      <div className="mt-1 text-muted-foreground">
        {run.requested_route_id ?? "Direct/manual"} →{" "}
        {run.selected_provider && run.selected_model
          ? `${run.selected_provider}/${run.selected_model}`
          : "—"}{" "}
        · Reasoning {run.reasoning_effort ?? "—"} · Fallback:{" "}
        {run.fallback_used == null ? "—" : run.fallback_used ? "yes" : "no"}
      </div>
      <div className="mt-1 text-muted-foreground">
        Evidence: ✓ {evidence} · Commit {run.commit_sha?.slice(0, 8) ?? "—"} · Evaluator confidence:{" "}
        {confidence}
      </div>
      <div className="mt-2 flex gap-2">
        <button
          className="rounded bg-primary px-2 py-1 text-primary-foreground disabled:opacity-50"
          onClick={() => void act("accept")}
          disabled={!hasEvaluation}
          title={
            hasEvaluation ? undefined : "Accept requires an automated evaluation to be present."
          }
        >
          <CheckIcon className="mr-1 inline size-3" />
          Accept
        </button>
        <button className="rounded border px-2 py-1" onClick={() => setEditing(true)}>
          <PencilIcon className="mr-1 inline size-3" />
          Adjust
        </button>
        <button className="rounded border px-2 py-1" onClick={() => void act("decline")}>
          <XIcon className="mr-1 inline size-3" />
          Decline
        </button>
        <button
          className="ml-auto underline"
          data-testid="outcome-review-later"
          onClick={postponeCard}
        >
          Review later
        </button>
      </div>
    </div>
  );
}
