import { useCallback, useEffect, useState } from "react";
import { CheckIcon, LoaderIcon, PencilIcon, XIcon } from "lucide-react";
import { getTaskRunForResponse, submitTaskRunReview } from "@/lib/taskOutcomes";
import type { TaskRunDetailResponse } from "@/lib/taskOutcomes";
import { TaskReviewCard } from "./TaskReviewCard";

/** Compact, response-keyed approval surface for completed harness tasks. */
export function TaskOutcomeBriefCard({
  sessionId,
  responseId,
}: {
  sessionId: string;
  responseId: string;
}) {
  const [detail, setDetail] = useState<TaskRunDetailResponse | null>(null);
  const [pending, setPending] = useState(true);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const load = useCallback(async () => {
    try {
      setDetail(await getTaskRunForResponse(sessionId, responseId));
      setError(null);
    } catch (e) {
      if (String(e).includes("404")) setDetail(null);
      else setError("Outcome brief unavailable");
    } finally {
      setPending(false);
    }
  }, [sessionId, responseId]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (attempt >= 3 || !detail || detail.evaluation) return;
    const timer = window.setTimeout(() => setAttempt((value) => value + 1), 800 * 2 ** attempt);
    return () => window.clearTimeout(timer);
  }, [attempt, detail]);
  if (pending)
    return (
      <div className="mt-2 text-xs text-muted-foreground" data-testid="outcome-brief-pending">
        <LoaderIcon className="mr-1 inline size-3 animate-spin" />
        Preparing outcome brief…
      </div>
    );
  if (!detail || error) return null;
  if (!detail.evaluation && attempt < 3)
    return (
      <div className="mt-2 text-xs text-muted-foreground" data-testid="outcome-brief-pending">
        Preparing outcome brief…
      </div>
    );
  const { run, evaluation, review } = detail;
  if (review && !editing)
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
  if (editing)
    return (
      <TaskReviewCard
        taskRunId={run.id}
        initialRun={run}
        isOpen
        onOpenChange={() => setEditing(false)}
        onReviewed={() => {
          setEditing(false);
          void load();
        }}
      />
    );
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
    await load();
  };
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
          className="rounded bg-primary px-2 py-1 text-primary-foreground"
          onClick={() => void act("accept")}
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
        <button className="ml-auto underline" onClick={() => void act("decline")}>
          Review later
        </button>
      </div>
    </div>
  );
}
