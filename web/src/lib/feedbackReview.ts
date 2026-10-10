import { diffFeedbackText } from "./feedbackChanges";

export type ChangeDecision = "accepted" | "rejected";
export type ChangeDecisions = Record<string, ChangeDecision>;
export interface ReviewDetails {
  outcome: string;
  comment: string;
  tags: string[];
}
export interface CommentEdit {
  key: string;
  before: string;
  after: string;
  changed: boolean;
}

/** Adjacent removal/insertion chunks form one independently reviewable edit. */
export function commentEdits(before: string, after: string): CommentEdit[] {
  const edits: CommentEdit[] = [];
  let offset = 0;
  for (const chunk of diffFeedbackText(before, after)) {
    const last = edits.at(-1);
    if (chunk.kind !== "unchanged" && last?.changed) {
      if (chunk.kind === "removed") last.before += chunk.text;
      else last.after += chunk.text;
      last.key += `:${chunk.kind}:${chunk.text}`;
    } else {
      edits.push({
        key: `comment:${offset}:${chunk.kind === "added" ? "" : chunk.text}:${chunk.kind === "removed" ? "" : chunk.text}`,
        before: chunk.kind === "added" ? "" : chunk.text,
        after: chunk.kind === "removed" ? "" : chunk.text,
        changed: chunk.kind !== "unchanged",
      });
    }
    offset += chunk.text.length;
  }
  return edits;
}
export const ratingChangeKey = (before: string, after: string) => `rating:${before}:${after}`;
export const tagChangeKey = (tag: string, adding: boolean) =>
  `tag:${adding ? "add" : "remove"}:${tag}`;
export function reviewChangeKeys(original: ReviewDetails, current: ReviewDetails): string[] {
  return [
    ...(original.outcome !== current.outcome
      ? [ratingChangeKey(original.outcome, current.outcome)]
      : []),
    ...original.tags.filter((t) => !current.tags.includes(t)).map((t) => tagChangeKey(t, false)),
    ...current.tags.filter((t) => !original.tags.includes(t)).map((t) => tagChangeKey(t, true)),
    ...commentEdits(original.comment, current.comment)
      .filter((e) => e.changed)
      .map((e) => e.key),
  ];
}
/** Pending edits remain proposals; rejected edits preserve the original value. */
export function reviewedFeedback(
  original: ReviewDetails,
  current: ReviewDetails,
  decisions: ChangeDecisions,
): ReviewDetails {
  return {
    outcome:
      decisions[ratingChangeKey(original.outcome, current.outcome)] === "rejected"
        ? original.outcome
        : current.outcome,
    tags: [
      ...original.tags.filter(
        (t) => current.tags.includes(t) || decisions[tagChangeKey(t, false)] === "rejected",
      ),
      ...current.tags.filter(
        (t) => !original.tags.includes(t) && decisions[tagChangeKey(t, true)] !== "rejected",
      ),
    ],
    comment: commentEdits(original.comment, current.comment)
      .map((e) => (decisions[e.key] === "rejected" ? e.before : e.after))
      .join(""),
  };
}
