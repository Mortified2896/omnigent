import { createContext, useContext, useEffect, useId, useMemo, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  useResponseFeedback,
  useSaveResponseFeedback,
  type ResponseFeedback,
} from "@/hooks/useResponseFeedback";
import type { Bubble } from "@/lib/renderItems";
import { LIVE_ITEM_PREFIX } from "@/lib/blocks";

import {
  useTaskExperiment,
  useSaveTaskOutcome,
  type TaskOutcome,
  type ExperimentEvent,
} from "@/hooks/useTaskExperiment";

const FeedbackContext = createContext<{
  sessionId: string;
  rows: Map<string, ResponseFeedback>;
  outcomes: Map<string, TaskOutcome>;
  outcomesReady: boolean;
  experiments: ExperimentEvent[];
  refreshExperiment: () => unknown;
  ready: boolean;
} | null>(null);

export function ResponseFeedbackProvider({
  sessionId,
  children,
}: {
  sessionId: string;
  children: React.ReactNode;
}) {
  const query = useResponseFeedback(sessionId);
  const experiment = useTaskExperiment(sessionId);
  const value = useMemo(
    () => ({
      sessionId,
      rows: new Map((query.data ?? []).map((row) => [row.response_id, row])),
      ready: query.isSuccess,
      outcomes: new Map(
        (experiment.data ?? [])
          .filter((row) => row.kind === "outcome" && row.outcome)
          .map((row) => [row.response_id, row.outcome!]),
      ),
      outcomesReady: experiment.isSuccess,
      experiments: experiment.data ?? [],
      refreshExperiment: experiment.refetch,
    }),
    [
      sessionId,
      query.data,
      query.isSuccess,
      experiment.data,
      experiment.isSuccess,
      experiment.refetch,
    ],
  );
  return <FeedbackContext.Provider value={value}>{children}</FeedbackContext.Provider>;
}

export function canRateResponse(bubble: Bubble): boolean {
  return (
    bubble.kind === "assistant" &&
    bubble.lifecycle === "completed" &&
    !bubble.continued &&
    bubble.items.some(
      (item) =>
        item.kind === "text" &&
        item.final &&
        item.text.trim() &&
        item.itemId &&
        !item.itemId.startsWith(LIVE_ITEM_PREFIX),
    )
  );
}

export function ResponseFeedbackActions({ responseId }: { responseId: string }) {
  const context = useContext(FeedbackContext);
  const refreshExperiment = context?.refreshExperiment;
  useEffect(() => {
    void refreshExperiment?.();
  }, [refreshExperiment, responseId]);
  if (!context) return null;
  return (
    <>
      <OutcomeEditor
        sessionId={context.sessionId}
        responseId={responseId}
        outcome={context.outcomes.get(responseId)}
        ready={context.outcomesReady}
      />
      <ExperimentAudit responseId={responseId} events={context.experiments} />
      <FeedbackEditor
        key={`${context.sessionId}:${responseId}`}
        sessionId={context.sessionId}
        responseId={responseId}
        feedback={context.rows.get(responseId)}
        ready={context.ready}
      />
    </>
  );
}

function FeedbackEditor({
  sessionId,
  responseId,
  feedback,
  ready,
}: {
  sessionId: string;
  responseId: string;
  feedback?: ResponseFeedback;
  ready: boolean;
}) {
  const mutation = useSaveResponseFeedback(sessionId, responseId);
  const [draft, setDraft] = useState<string | null>(null);
  const id = useId();
  const comment = draft ?? feedback?.comment ?? "";
  const disabled = !ready || mutation.isPending;
  return (
    <div className="contents" aria-label="Response feedback">
      {([1, -1] as const).map((rating) => (
        <Button
          key={rating}
          type="button"
          size="icon-sm"
          variant={feedback?.rating === rating ? "secondary" : "ghost"}
          className="min-h-10 min-w-10 md:min-h-7 md:min-w-7"
          aria-label={rating === 1 ? "Good response" : "Bad response"}
          aria-pressed={feedback?.rating === rating}
          disabled={disabled}
          onClick={() => mutation.mutate({ rating })}
        >
          {rating === 1 ? (
            <ThumbsUp aria-hidden="true" size={14} />
          ) : (
            <ThumbsDown aria-hidden="true" size={14} />
          )}
        </Button>
      ))}
      {feedback && (
        <div className="order-last flex w-full min-w-0 basis-full flex-wrap items-end gap-2">
          <label htmlFor={id} className="w-full text-xs">
            Why was this {feedback.rating === 1 ? "good" : "bad"}? (optional)
          </label>
          <textarea
            id={id}
            value={comment}
            maxLength={4000}
            rows={2}
            disabled={mutation.isPending}
            className="min-w-0 w-full rounded-md border bg-background p-2 text-sm"
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button
            type="button"
            size="sm"
            disabled={disabled || comment === (feedback.comment ?? "")}
            onClick={() =>
              mutation.mutate(
                { rating: feedback.rating, comment: comment || null },
                { onSuccess: () => setDraft(null) },
              )
            }
          >
            Save comment
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={disabled}
            onClick={() => mutation.mutate(null, { onSuccess: () => setDraft(null) })}
          >
            Clear feedback
          </Button>
        </div>
      )}
      {mutation.isPending && (
        <span role="status" className="text-xs">
          Saving…
        </span>
      )}
      {mutation.isError && (
        <span role="alert" className="order-last w-full text-xs text-destructive">
          Feedback was not saved. Please try again.
        </span>
      )}
      {!ready && (
        <span role="status" className="text-xs">
          Feedback unavailable
        </span>
      )}
    </div>
  );
}

const OUTCOMES: { value: TaskOutcome; label: string; definition: string }[] = [
  {
    value: "success",
    label: "Success",
    definition:
      "The requested task was accomplished on this attempt without a material correction or retry.",
  },
  {
    value: "partial",
    label: "Partial",
    definition:
      "Meaningful correct progress was made, but a material follow-up, correction, or additional implementation is required.",
  },
  {
    value: "failed",
    label: "Failed",
    definition:
      "The attempt did not accomplish the task or make sufficient correct progress to count as partial.",
  },
  {
    value: "not_sure",
    label: "Not sure",
    definition:
      "The outcome cannot yet be judged reliably. You can revise this after verification.",
  },
];
function OutcomeEditor({
  sessionId,
  responseId,
  outcome,
  ready,
}: {
  sessionId: string;
  responseId: string;
  outcome?: TaskOutcome;
  ready: boolean;
}) {
  const mutation = useSaveTaskOutcome(sessionId, responseId);
  return (
    <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Task outcome">
      {OUTCOMES.map((option) => (
        <Button
          key={option.value}
          type="button"
          size="sm"
          variant={outcome === option.value ? "secondary" : "ghost"}
          className="min-h-10 text-xs md:min-h-7"
          title={option.definition}
          aria-pressed={outcome === option.value}
          disabled={!ready || mutation.isPending}
          onClick={() => mutation.mutate(option.value)}
        >
          {option.label}
        </Button>
      ))}
      {mutation.isError && (
        <span role="alert" className="text-xs text-destructive">
          Task outcome was not saved. Please try again.
        </span>
      )}
    </div>
  );
}

function ExperimentAudit({
  responseId,
  events,
}: {
  responseId: string;
  events: ExperimentEvent[];
}) {
  const link = events.find((row) => row.kind === "response_link" && row.response_id === responseId);
  if (!link) return null;
  const forecast = events.find(
    (row) => row.kind === "forecast" && row.attempt_id === link.attempt_id,
  );
  const shadow = events.find(
    (row) => row.kind === "o3_shadow" && row.attempt_id === link.attempt_id,
  );
  if (!forecast) return null;
  return (
    <details
      className="order-last w-full min-w-0 basis-full text-xs"
      aria-label="Task experiment audit"
    >
      <summary className="cursor-pointer py-2">
        Task experiment{shadow ? " · O3 shadow (Experimental)" : ""}
      </summary>
      <div className="space-y-1 break-words rounded border p-2">
        <p>
          Human choice: {forecast.selected_model ?? "Default (unresolved)"} /{" "}
          {forecast.selected_reasoning_effort ?? "default effort"}
        </p>
        <p>
          Human P(success):{" "}
          {forecast.human_probability == null ? "Skipped" : `${forecast.human_probability}%`}
        </p>
        {forecast.experiment_source === "synthetic-acceptance" && (
          <p>Synthetic acceptance estimate; not a human forecast.</p>
        )}
        {shadow && (
          <>
            <p>
              O3 P(success):{" "}
              {shadow.status === "completed" ? `${shadow.probability}%` : "Unavailable"}
            </p>
            <p>{shadow.forecaster_id} · Uncalibrated estimate · Manual choice unchanged</p>
            {shadow.alternative && (
              <p>
                Suggested configuration: {shadow.alternative.canonical_model} /{" "}
                {shadow.alternative.compute_profile} ({shadow.alternative.probability}%)
              </p>
            )}
            {shadow.rationale && <p>{shadow.rationale}</p>}
          </>
        )}
      </div>
    </details>
  );
}
