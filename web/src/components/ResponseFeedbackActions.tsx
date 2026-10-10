import { FeedbackDiscussion } from "./FeedbackDiscussionTrigger";
import {
  FeedbackFormContext as FeedbackContext,
  type ReviewPerspectiveInput,
} from "./FeedbackContext";
import { useQuery } from "@tanstack/react-query";
import { useContext, useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { CheckIcon, PencilIcon } from "lucide-react";
import { cn } from "@/lib/utils";
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

export type { ReviewPerspectiveInput } from "./FeedbackContext";

export function ResponseFeedbackProvider({
  sessionId,
  hostId = null,
  renderPerspective,
  children,
}: {
  sessionId: string;
  hostId?: string | null;
  renderPerspective?: (responseId: string, review: ReviewPerspectiveInput) => ReactNode;
  children: React.ReactNode;
}) {
  const experiment = useTaskExperiment(sessionId);
  const value = useMemo(() => {
    const human = new Map<string, ExperimentEvent>();
    for (const row of experiment.data ?? []) {
      if (row.kind === "outcome" && row.outcome) human.set(row.response_id, row);
    }
    return { sessionId, hostId, human, ready: experiment.isSuccess, renderPerspective };
  }, [sessionId, hostId, experiment.data, experiment.isSuccess, renderPerspective]);
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

export function ResponseFeedbackActions({
  responseId,
  answerText,
  compact = true,
  autoSave = true,
  renderPerspective,
}: {
  responseId: string;
  answerText?: string;
  compact?: boolean;
  autoSave?: boolean;
  renderPerspective?: (review: ReviewPerspectiveInput) => ReactNode;
}) {
  const context = useContext(FeedbackContext);
  if (!context) return null;
  const human = context.human.get(responseId);
  return (
    <OutcomeEditor
      key={`${context.sessionId}:${responseId}:${autoSave ? "autosave" : (human?.id ?? "new")}`}
      sessionId={context.sessionId}
      hostId={context.hostId}
      responseId={responseId}
      human={human}
      ready={context.ready}
      compact={compact}
      autoSave={autoSave}
      renderPerspective={
        renderPerspective ??
        ((review) =>
          context.renderPerspective ? (
            context.renderPerspective(responseId, review)
          ) : (
            <FeedbackDiscussion responseId={responseId} review={review} answerText={answerText} />
          ))
      }
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

const COMPACT_TAG_LABELS: Record<string, string> = {
  "AGENTS instructions": "Instructions",
  "Task specification": "Task spec",
  "Environment/dependency": "Environment",
};

function OutcomeEditor({
  sessionId,
  hostId,
  responseId,
  human,
  ready,
  compact,
  autoSave,
  renderPerspective,
}: {
  sessionId: string;
  hostId: string | null;
  responseId: string;
  human?: ExperimentEvent;
  ready: boolean;
  compact: boolean;
  autoSave: boolean;
  renderPerspective?: (review: ReviewPerspectiveInput) => ReactNode;
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
  const [editingTags, setEditingTags] = useState(false);
  const [editingComment, setEditingComment] = useState(false);
  const commentId = useId();
  const [hydrated, setHydrated] = useState(!autoSave);
  useEffect(() => {
    if (autoSave && ready && !hydrated) {
      setComment(human?.comment ?? "");
      setTags(human?.tags ?? []);
      setHydrated(true);
    }
  }, [autoSave, ready, hydrated, human]);

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
    ((autoSave ? comment.trim() : comment) !== (human?.comment ?? "") ||
      JSON.stringify(tags) !== JSON.stringify(human?.tags ?? []));
  const { mutate, isPending, isError } = mutation;
  useEffect(() => {
    if (!autoSave || !hydrated || !outcome || !detailsChanged || isPending || isError) return;
    // Tags save immediately; wait briefly for a pause in typing comments.
    const delay = comment.trim() !== (human?.comment ?? "") ? 500 : 0;
    const timer = window.setTimeout(
      () => mutate({ outcome, comment: comment.trim() || null, tags }),
      delay,
    );
    return () => window.clearTimeout(timer);
  }, [
    autoSave,
    hydrated,
    outcome,
    detailsChanged,
    isPending,
    isError,
    comment,
    tags,
    human?.comment,
    mutate,
  ]);

  return (
    <div className="order-last flex w-full basis-full flex-col gap-2 py-1" aria-label="Task review">
      <div className={compact ? "-ml-2.5 flex flex-wrap items-center gap-2" : "contents"}>
        <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Task outcome">
          {!compact && <span className="mr-1 text-xs font-medium">Your outcome</span>}
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

        <ResponseScoringActions sessionId={sessionId} responseId={responseId} compact={compact} />
        {autoSave && (outcome || isPending || isError) && (
          <div
            className="ml-auto flex items-center gap-1 text-xs text-muted-foreground"
            role="status"
            aria-label="Feedback save status"
          >
            <span>{isError ? "Not saved" : isPending || detailsChanged ? "Saving…" : "Saved"}</span>
            {isError && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() =>
                  mutate({
                    outcome: mutation.variables?.outcome ?? outcome!,
                    comment: comment.trim() || null,
                    tags,
                  })
                }
              >
                Retry save
              </Button>
            )}
          </div>
        )}
      </div>

      <div className={compact ? "space-y-2" : "contents"}>
        {!compact && (outcome || excludedFromScoring) && (
          <ModelAttributionDetails
            compact={compact}
            responseId={responseId}
            sessionId={sessionId}
            hostId={hostId}
            attribution={human?.model_attribution ?? null}
          />
        )}

        {outcome && (
          <div
            className="space-y-2 md:rounded-md md:border md:border-border/70 md:p-2"
            data-testid="human-review-details"
          >
            <p className="hidden text-xs text-muted-foreground md:block">
              Tags and comments are for your review, not scoring-AI input.
            </p>
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
                    className={cn(
                      "min-h-10 px-2 text-xs md:h-7 md:min-h-7 md:text-[11px]",
                      !active && !editingTags && "hidden md:inline-flex",
                    )}
                    aria-pressed={active}
                    aria-label={tag}
                    title={tag}
                    disabled={mutation.isPending}
                    onClick={() => toggleTag(tag)}
                  >
                    {compact ? (COMPACT_TAG_LABELS[tag] ?? tag) : tag}
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
                    className="min-h-10 px-2 text-xs md:h-7 md:min-h-7 md:text-[11px]"
                    aria-pressed="true"
                    disabled={mutation.isPending}
                    onClick={() => toggleTag(tag)}
                  >
                    {tag} ×
                  </Button>
                ))}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="min-h-10 px-2 text-xs md:hidden"
                aria-expanded={editingTags}
                aria-controls={`feedback-tags-${responseId}`}
                onClick={() => setEditingTags((value) => !value)}
              >
                {editingTags ? "Done editing tags" : "Edit tags"}
              </Button>
            </div>
            <div
              id={`feedback-tags-${responseId}`}
              className={cn(
                "flex-wrap items-center gap-1.5 md:flex",
                editingTags || !autoSave ? "flex" : "hidden",
              )}
            >
              <input
                value={customTag}
                maxLength={64}
                className="h-10 min-w-0 flex-1 rounded-md border bg-background px-2 text-base md:h-8 md:min-w-36 md:text-xs"
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
                className="h-10 md:h-8"
                disabled={!customTag.trim() || tags.length >= 8 || mutation.isPending}
                onClick={addCustomTag}
              >
                Add tag
              </Button>
              {!autoSave && (
                <Button
                  type="button"
                  size="sm"
                  className="h-8"
                  disabled={!detailsChanged || mutation.isPending}
                  onClick={() =>
                    mutation.mutate({ outcome, comment: comment.trim() || null, tags })
                  }
                >
                  Save details
                </Button>
              )}
            </div>
            <div className="flex items-start gap-2 md:hidden">
              {!editingComment && comment && (
                <p className="min-w-0 flex-1 whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">
                  {comment}
                </p>
              )}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="min-h-10 shrink-0"
                aria-label={
                  editingComment ? "Done editing feedback comment" : "Edit feedback comment"
                }
                aria-expanded={editingComment}
                aria-controls={commentId}
                onClick={() => setEditingComment((value) => !value)}
              >
                <PencilIcon data-icon="inline-start" />
                {editingComment ? "Done" : comment ? "Edit" : "Add comment"}
              </Button>
            </div>
            <textarea
              id={commentId}
              value={comment}
              maxLength={4000}
              rows={2}
              disabled={!autoSave && mutation.isPending}
              className={cn(
                "min-w-0 w-full rounded-md border bg-background p-2 text-base md:block md:text-sm",
                !editingComment && "hidden",
              )}
              placeholder="Optional comment — what worked or what needs correction?"
              aria-label="Task review comment"
              onChange={(event) => setComment(event.target.value)}
            />
          </div>
        )}

        {outcome &&
          renderPerspective?.({
            outcome,
            comment,
            tags,
            saved: !detailsChanged && !mutation.isPending && !mutation.isError,
            onReplaceDetails: async (details) => {
              const nextComment = details.comment.slice(0, 4000);
              const nextTags = [...new Set(details.tags)].slice(0, 8);
              setComment(nextComment);
              setTags(nextTags);
              await mutation.mutateAsync({
                outcome: details.outcome ?? outcome,
                comment: nextComment.trim() || null,
                tags: nextTags,
              });
            },
            onAppendComment: (text) =>
              setComment((current) => {
                const addition = text.trim();
                if (!addition || current.includes(addition)) return current;
                return [current.trim(), addition].filter(Boolean).join("\n\n").slice(0, 4000);
              }),
            onAddTag: (tag) =>
              setTags((current) =>
                current.length < 8 && !current.includes(tag) ? [...current, tag] : current,
              ),
          })}

        {compact && outcome && (
          <details className="border-t border-border/70 md:rounded-lg md:border">
            <summary className="cursor-pointer py-2 text-xs font-medium text-muted-foreground md:px-3">
              Show model decision
            </summary>
            <div className="px-2 pb-2">
              <ModelAttributionDetails
                compact
                responseId={responseId}
                sessionId={sessionId}
                hostId={hostId}
                attribution={human?.model_attribution ?? null}
              />
            </div>
          </details>
        )}
      </div>
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
  compact = false,
}: {
  responseId: string;
  sessionId: string;
  hostId: string | null;
  attribution: ExperimentEvent["model_attribution"];
  compact?: boolean;
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
  const assignedChoice = round
    ? choiceLabel(round, review?.assigned_choice_id ?? review?.assigned_candidate_id)
    : null;

  if (compact) {
    const pool = [
      ...(round?.decision_context?.qualified_pool ?? []),
      ...(round?.decision_context?.user_enabled_pool ?? []),
      ...(round?.decision_context?.advisor_visible_pool ?? []),
    ];
    const humanId = review?.human_choice_id ?? review?.human_candidate_id;
    const advisorId = review?.advisor_choice_id ?? review?.advisor_candidate_id;
    const selectedId = review?.assigned_choice_id ?? review?.assigned_candidate_id;
    const human = pool.find((choice) => choice.choice_id === humanId);
    const advisor = pool.find((choice) => choice.choice_id === advisorId);
    const modelName = (id: string) =>
      id
        .replace(/^gpt-/, "GPT-")
        .replace(
          /-(sol|astra|luna)$/i,
          (_, family: string) => ` ${family[0].toUpperCase()}${family.slice(1)}`,
        );
    const options = [
      {
        title: "Your choice",
        model: human?.model_id ?? responseAttribution?.requested_model,
        effort: human?.reasoning_effort ?? responseAttribution?.reasoning_effort,
        selected: Boolean(
          review
            ? humanId && selectedId === humanId
            : !roundId &&
                responseAttribution?.actual_model &&
                responseAttribution.actual_model === responseAttribution.requested_model,
        ),
      },
      {
        title: "Advisor’s choice",
        model: advisor?.model_id,
        effort: advisor?.reasoning_effort,
        selected: Boolean(advisorId && selectedId === advisorId),
      },
    ];
    return (
      <section
        className="space-y-3 rounded-lg border border-border/70 bg-muted/20 p-3"
        aria-label="Model attribution"
        data-testid="model-attribution"
      >
        <div className="grid grid-cols-1 gap-2 min-[480px]:grid-cols-2">
          {options.map((option) => (
            <div
              key={option.title}
              data-selected={option.selected}
              aria-label={`${option.title}${option.selected ? " · Selected" : ""}`}
              className={cn(
                "min-w-0 rounded-md border bg-background p-3",
                option.selected ? "border-primary ring-1 ring-primary" : "border-border/60",
              )}
            >
              <div className="mb-1 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                <span>{option.title}</span>
                {option.selected && (
                  <CheckIcon className="size-4 shrink-0 text-primary" aria-label="Selected" />
                )}
              </div>
              <p className="break-words text-sm font-semibold">
                {option.model
                  ? modelName(option.model)
                  : option.title === "Advisor’s choice" && !roundId
                    ? "Not used"
                    : "Unavailable"}
              </p>
              {option.effort && (
                <p className="mt-1 text-xs capitalize text-muted-foreground">
                  {option.effort} reasoning
                </p>
              )}
            </div>
          ))}
        </div>
        {review?.rationale && (
          <div className="text-sm">
            <p className="mb-1 text-xs font-medium text-muted-foreground">
              Why the Advisor recommended it
            </p>
            <p>{review.rationale}</p>
          </div>
        )}
        {review?.overridden && review.override_reason && (
          <p className="text-xs text-muted-foreground">Override: {review.override_reason}</p>
        )}
        {roundQuery.isLoading && roundId && (
          <p className="text-xs text-muted-foreground">Loading Advisor decision…</p>
        )}
        {roundId && (!hostId || roundQuery.isError) && (
          <p className="text-xs text-muted-foreground">Advisor decision details are unavailable.</p>
        )}
        {attributionQuery.isLoading && (
          <p className="text-xs text-muted-foreground">Loading execution details…</p>
        )}
        {attributionQuery.isError && (
          <p className="text-xs text-muted-foreground">Execution details are unavailable.</p>
        )}
      </section>
    );
  }

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
      {responseAttribution && !roundId && (
        <div className="space-y-1 text-xs text-muted-foreground">
          <p>Model Advisor was not used for this response.</p>
          <p>Turn on Advisor before sending a message to record its model choice and reasoning.</p>
        </div>
      )}
      {roundId && !hostId && (
        <p className="text-xs text-muted-foreground">
          Advisor decision details are unavailable because this session has no host binding.
        </p>
      )}
      {review && (
        <div className="space-y-1 text-xs text-muted-foreground">
          {humanChoice && <p>Your choice: {humanChoice}</p>}
          {advisorChoice && <p>Advisor recommendation: {advisorChoice}</p>}
          {assignedChoice && <p>Assigned model: {assignedChoice}</p>}
          <p>Advisor reasoning: {review.rationale}</p>
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
