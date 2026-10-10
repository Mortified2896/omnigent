import { useMemo } from "react";
import { FeedbackChoiceButton, feedbackTagLabel, FEEDBACK_OUTCOMES } from "./FeedbackChoiceButton";
import { feedbackOutcomeLabel } from "@/lib/feedbackChanges";

import {
  commentEdits,
  ratingChangeKey,
  tagChangeKey,
  reviewedFeedback,
  type ChangeDecisions,
  type ChangeDecision,
} from "@/lib/feedbackReview";
import { FeedbackChangeControl } from "./FeedbackChangeControl";

interface PreviewDetails {
  outcome: string;
  comment: string;
  tags: string[];
}

const NO_DECISIONS: ChangeDecisions = {};

const ADDED =
  "bg-success/10 text-foreground underline decoration-success decoration-2 underline-offset-2";
const REMOVED =
  "bg-destructive/10 text-muted-foreground line-through decoration-destructive decoration-2";

function MarkedText({ kind, text }: { kind: "unchanged" | "removed" | "added"; text: string }) {
  if (kind === "added")
    return (
      <ins className={ADDED} aria-label={`Added: ${text}`}>
        {text}
      </ins>
    );
  if (kind === "removed")
    return (
      <del className={REMOVED} aria-label={`Removed: ${text}`}>
        {text}
      </del>
    );
  return text;
}

/** Word-style change marks use the same values that explicit acceptance saves. */
export function SuggestedFeedbackPreview({
  original,
  current,
  trackChanges,
  decisions = NO_DECISIONS,
  onDecide,
  tagsOnly = false,
}: {
  original: PreviewDetails;
  current: PreviewDetails;
  trackChanges: boolean;
  tagsOnly?: boolean;
  decisions?: ChangeDecisions;
  onDecide?: (key: string, decision: ChangeDecision) => void;
}) {
  const commentChanges = useMemo(
    () => commentEdits(original.comment, current.comment),
    [original.comment, current.comment],
  );
  const resolved = reviewedFeedback(original, current, decisions);
  const review = (
    key: string,
    label: string,
    before: string,
    after: string,
    choice?: { kind: "outcome" | "tag"; selected: boolean },
  ) => {
    const decision = decisions[key];
    const content =
      decision === "accepted" ? (
        after
      ) : decision === "rejected" ? (
        before
      ) : (
        <>
          {before && <MarkedText kind="removed" text={String(before)} />}
          {after && <MarkedText kind="added" text={String(after)} />}
        </>
      );
    return onDecide ? (
      <FeedbackChangeControl
        label={label}
        decision={decision}
        onDecide={(value) => onDecide(key, value)}
        choice={choice}
      >
        {content ||
          (choice ? (
            before || after
          ) : (
            <span className="text-xs text-muted-foreground">
              {decision === "accepted" ? "Removed" : "Addition rejected"}
            </span>
          ))}
      </FeedbackChangeControl>
    ) : (
      content
    );
  };
  const tags = trackChanges
    ? [
        ...original.tags.map((tag) => ({
          tag,
          kind: current.tags.includes(tag) ? ("unchanged" as const) : ("removed" as const),
        })),
        ...current.tags
          .filter((tag) => !original.tags.includes(tag))
          .map((tag) => ({
            tag,
            kind: "added" as const,
          })),
      ]
    : resolved.tags.map((tag) => ({ tag, kind: "unchanged" as const }));
  const ratingChanged = trackChanges && current.outcome !== original.outcome;
  const ratingKey = ratingChangeKey(original.outcome, current.outcome);
  const ratingDecision = decisions[ratingKey];
  return (
    <div className="flex min-w-0 flex-col gap-2.5">
      {!tagsOnly && (
        <div className="flex flex-wrap items-center gap-3" aria-label="Proposed feedback rating">
          <span className="w-10 shrink-0 text-xs text-muted-foreground">Rating</span>
          <div className="flex flex-wrap gap-1">
            {FEEDBACK_OUTCOMES.map((option) => {
              const isOriginal = option.value === original.outcome;
              const isProposed = option.value === current.outcome;
              const selected =
                option.value ===
                (ratingChanged && !ratingDecision ? original.outcome : resolved.outcome);
              const content =
                ratingChanged && !ratingDecision && (isOriginal || isProposed) ? (
                  <MarkedText kind={isProposed ? "added" : "removed"} text={option.label} />
                ) : (
                  option.label
                );
              return ratingChanged && isProposed && onDecide ? (
                <FeedbackChangeControl
                  key={option.value}
                  label="rating change"
                  decision={ratingDecision}
                  onDecide={(value) => onDecide(ratingKey, value)}
                  choice={{ kind: "outcome", selected }}
                >
                  {content}
                </FeedbackChangeControl>
              ) : (
                <FeedbackChoiceButton
                  key={option.value}
                  kind="outcome"
                  selected={selected}
                  title={option.definition}
                  aria-disabled="true"
                  tabIndex={-1}
                  aria-label={
                    ratingChanged && isOriginal
                      ? `Original rating ${feedbackOutcomeLabel(original.outcome)}`
                      : undefined
                  }
                >
                  {content}
                </FeedbackChoiceButton>
              );
            })}
          </div>
        </div>
      )}
      {tags.length > 0 && (
        <div className="flex items-start gap-3">
          <span className="w-10 shrink-0 pt-1 text-xs text-muted-foreground">Tags</span>
          <div
            className="flex min-w-0 flex-wrap items-center gap-1.5"
            aria-label="Proposed feedback tags"
          >
            {tags.map(({ tag, kind }) =>
              kind === "unchanged" ? (
                <FeedbackChoiceButton
                  key={tag}
                  kind="tag"
                  selected
                  aria-disabled="true"
                  tabIndex={-1}
                  className="h-auto max-w-full whitespace-normal break-words"
                >
                  {feedbackTagLabel(tag)}
                </FeedbackChoiceButton>
              ) : (
                <span key={`${kind}:${tag}`} className="min-w-0 max-w-full break-words text-sm">
                  {review(
                    tagChangeKey(tag, kind === "added"),
                    `${kind === "added" ? "add" : "remove"} tag ${tag}`,
                    kind === "removed" ? feedbackTagLabel(tag) : "",
                    kind === "added" ? feedbackTagLabel(tag) : "",
                    {
                      kind: "tag",
                      selected: decisions[tagChangeKey(tag, kind === "added")]
                        ? resolved.tags.includes(tag)
                        : original.tags.includes(tag),
                    },
                  )}
                </span>
              ),
            )}
          </div>
        </div>
      )}
      {!tagsOnly && (
        <p
          className="whitespace-pre-wrap break-words text-ui leading-relaxed [&>del+ins]:ms-1 [&>ins+del]:ms-1"
          aria-label="Suggested feedback comment preview"
        >
          {trackChanges
            ? commentChanges.length
              ? commentChanges.map((change, index) => (
                  <span key={change.key}>
                    {change.changed
                      ? review(
                          change.key,
                          `comment change ${index + 1}`,
                          change.before,
                          change.after,
                        )
                      : change.after}
                  </span>
                ))
              : "No comment"
            : resolved.comment || "No comment"}
        </p>
      )}
    </div>
  );
}
