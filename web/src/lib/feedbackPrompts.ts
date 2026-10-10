import type { FeedbackDiscussionIntent } from "@/components/FeedbackDiscussionControls";
import type { Original } from "@/components/FeedbackContext";
import type { AnyBlock } from "./blocks";

export const FEEDBACK_CONTEXT_MARKER = "\n\nFeedback review context:\n";
export const FEEDBACK_PROMPTS = {
  discuss: "Discuss my feedback on this answer. Do you agree with my assessment?",
  suggest:
    "Review my feedback on this answer. Suggest a rating, clearer wording, and tags, with reasons for any changes.",
  inspect:
    "Inspect your actions and tool results for this answer. What was verified, and what is still unverified?",
};
export interface FeedbackTarget {
  responseId: string;
  intent: FeedbackDiscussionIntent;
  original: Original;
}
export interface PreparedFeedback extends FeedbackTarget {
  id: string;
  answerExcerpt?: string;
}

export function feedbackRequest(text: string, target: FeedbackTarget, answerExcerpt?: string) {
  return (
    text +
    FEEDBACK_CONTEXT_MARKER +
    JSON.stringify({
      response_id: target.responseId,
      intent: target.intent,
      original: target.original,
      selected_answer_excerpt: answerExcerpt?.slice(0, 2000),
      instructions:
        "Discuss the selected answer without tools or project changes. My outcome is authoritative. You may propose success, partial, failed, or not_sure, with reasons. Do not change saved feedback. For proposed edits include a fenced feedback-json block with outcome, comment (at most 4000 characters), and tags (at most 8 of at most 64 characters). Every edit needs my explicit acceptance.",
    })
  );
}

export function readFeedbackTarget(text: string): FeedbackTarget | null {
  const marker = text.lastIndexOf(FEEDBACK_CONTEXT_MARKER);
  if (marker < 0) return null;
  try {
    const value = JSON.parse(text.slice(marker + FEEDBACK_CONTEXT_MARKER.length));
    if (
      typeof value.response_id !== "string" ||
      !["discuss", "suggest", "inspect"].includes(value.intent) ||
      !value.original ||
      !["success", "partial", "failed", "not_sure"].includes(value.original.outcome) ||
      typeof value.original.comment !== "string" ||
      !Array.isArray(value.original.tags) ||
      value.original.tags.length > 8 ||
      value.original.tags.some((tag: unknown) => typeof tag !== "string" || tag.length > 64)
    )
      return null;
    return { responseId: value.response_id, intent: value.intent, original: value.original };
  } catch {
    return null;
  }
}

/** Keep the generated review metadata out of the visible user message. */
export function feedbackVisibleText(text: string): string {
  return readFeedbackTarget(text) ? text.slice(0, text.lastIndexOf(FEEDBACK_CONTEXT_MARKER)) : text;
}

/** Bind each review reply to the preceding user request, including after reload. */
export function feedbackReplyTargets(blocks: AnyBlock[]): Map<string, FeedbackTarget> {
  const result = new Map<string, FeedbackTarget>();
  let target: FeedbackTarget | null = null;
  for (const block of blocks) {
    if (block.type === "user_message") {
      target = readFeedbackTarget(
        block.content
          .filter((part) => part.type === "input_text")
          .map((part) => part.text)
          .join("\n"),
      );
    } else if (target && block.ctx?.responseId) result.set(block.ctx.responseId, target);
  }
  return result;
}
