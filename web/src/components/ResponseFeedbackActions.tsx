import { createContext, useContext, useMemo } from "react";
import { Button } from "@/components/ui/button";
import type { Bubble } from "@/lib/renderItems";
import { LIVE_ITEM_PREFIX } from "@/lib/blocks";
import { useTaskExperiment, useSaveTaskOutcome, type TaskOutcome } from "@/hooks/useTaskExperiment";

const FeedbackContext = createContext<{
  sessionId: string;
  outcomes: Map<string, TaskOutcome>;
  ready: boolean;
} | null>(null);

export function ResponseFeedbackProvider({
  sessionId,
  children,
}: {
  sessionId: string;
  children: React.ReactNode;
}) {
  const experiment = useTaskExperiment(sessionId);
  const value = useMemo(
    () => ({
      sessionId,
      outcomes: new Map(
        (experiment.data ?? [])
          .filter((row) => row.kind === "outcome" && row.outcome)
          .map((row) => [row.response_id, row.outcome!]),
      ),
      ready: experiment.isSuccess,
    }),
    [sessionId, experiment.data, experiment.isSuccess],
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
  if (!context) return null;
  return (
    <OutcomeEditor
      sessionId={context.sessionId}
      responseId={responseId}
      outcome={context.outcomes.get(responseId)}
      ready={context.ready}
    />
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
