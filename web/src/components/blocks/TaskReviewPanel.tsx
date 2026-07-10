// Outcome-review panel for a session.
//
// Loads the session's terminalised but-unreviewed task outcomes
// on mount + on refresh, and renders one
// :class:`TaskReviewCard` per run. Hidden when the session has
// no terminalised runs yet, so it doesn't add chrome to short
// chat sessions.
//
// Mounted by ChatPage as a side panel (NOT inline in the bubble
// stream): reviews are post-turn work, not part of the chat
// transcript. An "Outcome not reviewed (N)" pill in the chat
// header links into the panel so the user can find it without
// scrolling.

import { useCallback, useEffect, useState } from "react";
import { LoaderIcon, RefreshCwIcon } from "lucide-react";
import { listUnreviewedTaskOutcomes } from "@/lib/taskOutcomes";
import type { TaskRunSummary } from "@/lib/taskOutcomes";
import { TaskReviewCard } from "./TaskReviewCard";

export interface TaskReviewPanelProps {
  sessionId: string;
  /** When ``true`` the panel auto-refreshes on every render — used
   * by the chat header pill to keep the count in sync. */
  refreshKey?: number;
  onCountChange?: (count: number) => void;
}

export function TaskReviewPanel({ sessionId, refreshKey, onCountChange }: TaskReviewPanelProps) {
  const [unreviewed, setUnreviewed] = useState<TaskRunSummary[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await listUnreviewedTaskOutcomes(sessionId);
      setUnreviewed(resp.runs);
      onCountChange?.(resp.runs.length);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [sessionId, onCountChange]);

  useEffect(() => {
    refresh();
  }, [refresh, refreshKey]);

  if (loading && unreviewed.length === 0) {
    return (
      <div
        className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm text-slate-500 shadow-sm"
        data-testid="task-review-panel-loading"
      >
        <LoaderIcon className="h-4 w-4 animate-spin" />
        Loading outcome review queue…
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
        Failed to load outcomes: {error}
      </div>
    );
  }
  if (unreviewed.length === 0) {
    return (
      <div
        className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-2 text-xs text-slate-500"
        data-testid="task-review-panel-empty"
      >
        No outcomes waiting for review.
      </div>
    );
  }
  return (
    <div className="grid gap-2" data-testid="task-review-panel">
      <div className="flex items-center justify-between px-1">
        <div className="text-xs font-medium text-slate-500">
          {unreviewed.length} outcome{unreviewed.length === 1 ? "" : "s"} waiting for review
        </div>
        <button
          type="button"
          onClick={refresh}
          className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-100"
          aria-label="Refresh outcome review queue"
        >
          <RefreshCwIcon className="h-3 w-3" />
          Refresh
        </button>
      </div>
      {unreviewed.map((run) => (
        <TaskReviewCard
          key={run.id}
          taskRunId={run.id}
          initialRun={run as unknown as import("@/lib/taskOutcomes").TaskRun}
          onReviewed={() => {
            // Refresh after a save — runs that just got reviewed
            // drop out of the unreviewed list.
            void refresh();
          }}
        />
      ))}
    </div>
  );
}
