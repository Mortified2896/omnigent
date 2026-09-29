import { useQuery } from "@tanstack/react-query";
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
import { ResponseScoringActions } from "./ResponseScoringActions";
import { fetchRound, type RoundDto } from "@/lib/modelAdvisorApi";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";
import { useSessionScoringPolicy } from "@/hooks/useScoringEligibility";

const FeedbackContext = createContext<{
  sessionId: string;
  hostId: string | null;
  human: Map<string, ExperimentEvent>;
  ready: boolean;
} | null>(null);

export function ResponseFeedbackProvider({
  sessionId,
  hostId = null,
  children,
}: {
  sessionId: string;
  hostId?: string | null;
  children: React.ReactNode;
}) {
  const experiment = useTaskExperiment(sessionId);
  const value = useMemo(() => {
    const human = new Map<string, ExperimentEvent>();
    for (const row of experiment.data ?? []) {
      if (row.kind === "outcome" && row.outcome) human.set(row.response_id, row);
    }
    return { sessionId, hostId, human, ready: experiment.isSuccess };
  }, [sessionId, hostId, experiment.data, experiment.isSuccess]);
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
      hostId={context.hostId}
      responseId={responseId}
      human={human}
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

function OutcomeEditor({
  sessionId,
  hostId,
  responseId,
  human,
  ready,
}: {
  sessionId: string;
  hostId: string | null;
  responseId: string;
  human?: ExperimentEvent;
  ready: boolean;
}) {
  const mutation = useSaveTaskOutcome(sessionId, responseId);
  const scoringPolicy = useSessionScoringPolicy(sessionId);
  const excludedFromScoring =
    scoringPolicy.data?.score_eligible === false ||
    scoringPolicy.data?.responses?.[responseId]?.score_eligible === false;
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

      <ResponseScoringActions sessionId={sessionId} responseId={responseId} />

      {(outcome || excludedFromScoring) && (
        <ModelAttributionDetails
          responseId={responseId}
          sessionId={sessionId}
          hostId={hostId}
          attribution={human?.model_attribution ?? null}
        />
      )}

      {outcome && (
        <div
          className="space-y-2 rounded-md border border-border/70 p-2"
          data-testid="human-review-details"
        >
          <p className="text-xs text-muted-foreground">
            Tags and comments are for your review, not scoring-AI input.
          </p>
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

function choiceLabel(round: RoundDto, choiceId: string | undefined): string | null {
  if (!choiceId) return null;
  const pools = [
    round.decision_context?.qualified_pool ?? [],
    round.decision_context?.user_enabled_pool ?? [],
    round.decision_context?.advisor_visible_pool ?? [],
  ];
  const choice = pools.flat().find((candidate) => candidate.choice_id === choiceId);
  return choice ? `${choice.model_id} · ${choice.provider} · ${choice.reasoning_effort}` : choiceId;
}

function ModelAttributionDetails({
  responseId,
  sessionId,
  hostId,
  attribution,
}: {
  responseId: string;
  sessionId: string;
  hostId: string | null;
  attribution: ExperimentEvent["model_attribution"];
}) {
  const attributionQuery = useQuery({
    queryKey: ["response-model-attribution", sessionId, responseId],
    queryFn: async () => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/response-attribution/${encodeURIComponent(responseId)}`,
      );
      if (!response.ok) throw new Error("Could not load response model attribution");
      return (await response.json()) as NonNullable<ExperimentEvent["model_attribution"]> | null;
    },
    enabled: !attribution,
    staleTime: Infinity,
    retry: false,
  });
  const responseAttribution = attribution ?? attributionQuery.data ?? null;
  const roundId = responseAttribution?.advisor_round_id ?? null;
  const roundQuery = useQuery({
    queryKey: ["model-advisor-round", getCurrentUserId(), hostId, roundId],
    queryFn: () => fetchRound(hostId!, roundId!),
    enabled: Boolean(hostId && roundId),
    staleTime: Infinity,
    retry: false,
  });
  const round = roundQuery.data;
  const review = round?.review;
  const armLabel =
    review?.assigned_arm === "same"
      ? "Your choice and the advisor agreed"
      : review?.assigned_arm === "advisor"
        ? "Advisor’s choice was selected"
        : review?.assigned_arm === "human"
          ? "Your choice was selected"
          : null;
  const humanChoice = round
    ? choiceLabel(round, review?.human_choice_id ?? review?.human_candidate_id)
    : null;
  const advisorChoice = round
    ? choiceLabel(round, review?.advisor_choice_id ?? review?.advisor_candidate_id)
    : null;

  return (
    <section
      className="space-y-1 rounded-md border border-border/70 bg-muted/20 p-2"
      aria-label="Model attribution"
      data-testid="model-attribution"
    >
      <p className="text-xs font-medium">Model and decision</p>
      {responseAttribution?.actual_model ? (
        <p className="text-xs">
          Reported model used:{" "}
          <span className="font-medium">{responseAttribution.actual_model}</span>
        </p>
      ) : responseAttribution?.model_status === "unknown" ? (
        <p className="text-xs text-muted-foreground">
          The harness did not report the model used for this response.
        </p>
      ) : attributionQuery.isLoading ? (
        <p className="text-xs text-muted-foreground">Loading response model details…</p>
      ) : attributionQuery.isError ? (
        <p className="text-xs text-muted-foreground">Response model details are unavailable.</p>
      ) : (
        <p className="text-xs text-muted-foreground">
          No execution details were saved for this response.
        </p>
      )}
      {responseAttribution?.requested_model && (
        <p className="text-xs text-muted-foreground">
          Configured model: {responseAttribution.requested_model}
        </p>
      )}
      {responseAttribution?.reasoning_effort && (
        <p className="text-xs text-muted-foreground">
          Requested reasoning: {responseAttribution.reasoning_effort}
        </p>
      )}
      {armLabel && <p className="text-xs">{armLabel}</p>}
      {roundQuery.isLoading && roundId && (
        <p className="text-xs text-muted-foreground">Loading advisor rationale…</p>
      )}
      {review && (
        <div className="space-y-1 text-xs text-muted-foreground">
          {humanChoice && <p>Your choice: {humanChoice}</p>}
          {advisorChoice && <p>Advisor recommendation: {advisorChoice}</p>}
          <p>Reasoning: {review.rationale}</p>
          {review.overridden && review.override_reason && <p>Override: {review.override_reason}</p>}
        </div>
      )}
      {roundQuery.isError && roundId && (
        <p className="text-xs text-muted-foreground">Advisor rationale is unavailable.</p>
      )}
      <p className="text-xs text-muted-foreground">
        Response / trace key: <code className="break-all">{responseId}</code>
      </p>
    </section>
  );
}
