import { createContext, useContext, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import type { Bubble } from "@/lib/renderItems";
import { LIVE_ITEM_PREFIX } from "@/lib/blocks";
import {
  useTaskExperiment,
  useSaveTaskOutcome,
  type ExperimentEvent,
  type TaskOutcome,
} from "@/hooks/useTaskExperiment";

const FeedbackContext = createContext<{
  sessionId: string;
  human: Map<string, ExperimentEvent>;
  model: Map<string, ExperimentEvent>;
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
  const value = useMemo(() => {
    const human = new Map<string, ExperimentEvent>();
    const model = new Map<string, ExperimentEvent>();
    for (const row of experiment.data ?? []) {
      if (row.kind === "outcome" && row.outcome) human.set(row.response_id, row);
      if (row.kind === "model_review" && row.outcome) model.set(row.response_id, row);
    }
    return { sessionId, human, model, ready: experiment.isSuccess };
  }, [sessionId, experiment.data, experiment.isSuccess]);
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
  const human = context.human.get(responseId);
  return (
    <OutcomeEditor
      key={`${context.sessionId}:${responseId}:${human?.id ?? "new"}`}
      sessionId={context.sessionId}
      responseId={responseId}
      human={human}
      modelReview={context.model.get(responseId)}
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

const REVIEW_TAGS = [
  "AGENTS instructions",
  "Documentation",
  "Task specification",
  "Routing/floor",
  "Model capability",
  "Tool/harness",
  "Environment/dependency",
  "Tests/verification",
] as const;

function titleCaseOutcome(value: TaskOutcome): string {
  return value === "not_sure" ? "Not sure" : value.charAt(0).toUpperCase() + value.slice(1);
}

function OutcomeEditor({
  sessionId,
  responseId,
  human,
  modelReview,
  ready,
}: {
  sessionId: string;
  responseId: string;
  human?: ExperimentEvent;
  modelReview?: ExperimentEvent;
  ready: boolean;
}) {
  const mutation = useSaveTaskOutcome(sessionId, responseId);
  const outcome = human?.outcome;
  const [comment, setComment] = useState(human?.comment ?? "");
  const [tags, setTags] = useState<string[]>(human?.tags ?? []);
  const [customTag, setCustomTag] = useState("");

  function toggleTag(tag: string): void {
    setTags((current) =>
      current.some((value) => value.toLocaleLowerCase() === tag.toLocaleLowerCase())
        ? current.filter((value) => value.toLocaleLowerCase() !== tag.toLocaleLowerCase())
        : current.length < 8
          ? [...current, tag]
          : current,
    );
  }

  function addCustomTag(): void {
    const tag = customTag.trim().replace(/\s+/g, " ");
    if (!tag || tag.length > 64 || tags.length >= 8) return;
    if (!tags.some((value) => value.toLocaleLowerCase() === tag.toLocaleLowerCase())) {
      setTags((current) => [...current, tag]);
    }
    setCustomTag("");
  }

  const detailsChanged =
    outcome !== undefined &&
    (comment !== (human?.comment ?? "") ||
      JSON.stringify(tags) !== JSON.stringify(human?.tags ?? []));

  return (
    <div className="order-last flex w-full basis-full flex-col gap-2 py-1" aria-label="Task review">
      {modelReview?.outcome && (
        <div
          className="rounded-md border border-border bg-muted/30 px-2.5 py-2 text-xs"
          data-testid="model-self-review"
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">Model self-review</span>
            <span>{titleCaseOutcome(modelReview.outcome)}</span>
            {typeof modelReview.confidence === "number" && (
              <span className="text-muted-foreground">
                {Math.round(modelReview.confidence * 100)}%
              </span>
            )}
          </div>
          {(modelReview.tags ?? []).length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {(modelReview.tags ?? []).map((tag) => (
                <span key={tag} className="rounded-full border px-1.5 py-0.5 text-[11px]">
                  {tag}
                </span>
              ))}
            </div>
          )}
          {modelReview.comment && (
            <p className="mt-1 text-muted-foreground">{modelReview.comment}</p>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Task outcome">
        <span className="mr-1 text-xs font-medium">Your outcome</span>
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
            onClick={() =>
              mutation.mutate({
                outcome: option.value,
                comment: comment.trim() || null,
                tags,
              })
            }
          >
            {option.label}
          </Button>
        ))}
      </div>

      {outcome && (
        <div
          className="space-y-2 rounded-md border border-border/70 p-2"
          data-testid="human-review-details"
        >
          <textarea
            value={comment}
            maxLength={4000}
            rows={2}
            disabled={mutation.isPending}
            className="min-w-0 w-full rounded-md border bg-background p-2 text-sm"
            placeholder="Optional comment — what worked or what needs correction?"
            aria-label="Task review comment"
            onChange={(event) => setComment(event.target.value)}
          />
          <div className="flex flex-wrap gap-1" aria-label="Task review tags">
            {REVIEW_TAGS.map((tag) => {
              const active = tags.some(
                (value) => value.toLocaleLowerCase() === tag.toLocaleLowerCase(),
              );
              return (
                <Button
                  key={tag}
                  type="button"
                  size="sm"
                  variant={active ? "secondary" : "outline"}
                  className="h-7 px-2 text-[11px]"
                  aria-pressed={active}
                  disabled={mutation.isPending}
                  onClick={() => toggleTag(tag)}
                >
                  {tag}
                </Button>
              );
            })}
            {tags
              .filter(
                (tag) =>
                  !REVIEW_TAGS.some(
                    (known) => known.toLocaleLowerCase() === tag.toLocaleLowerCase(),
                  ),
              )
              .map((tag) => (
                <Button
                  key={tag}
                  type="button"
                  size="sm"
                  variant="secondary"
                  className="h-7 px-2 text-[11px]"
                  aria-pressed="true"
                  disabled={mutation.isPending}
                  onClick={() => toggleTag(tag)}
                >
                  {tag} ×
                </Button>
              ))}
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <input
              value={customTag}
              maxLength={64}
              className="h-8 min-w-36 flex-1 rounded-md border bg-background px-2 text-xs"
              placeholder="Custom tag"
              aria-label="Custom task review tag"
              onChange={(event) => setCustomTag(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  addCustomTag();
                }
              }}
            />
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-8"
              disabled={!customTag.trim() || tags.length >= 8 || mutation.isPending}
              onClick={addCustomTag}
            >
              Add tag
            </Button>
            <Button
              type="button"
              size="sm"
              className="h-8"
              disabled={!detailsChanged || mutation.isPending}
              onClick={() => mutation.mutate({ outcome, comment: comment.trim() || null, tags })}
            >
              Save details
            </Button>
          </div>
        </div>
      )}

      {mutation.isError && (
        <span role="alert" className="text-xs text-destructive">
          Task review was not saved. Please try again.
        </span>
      )}
    </div>
  );
}
