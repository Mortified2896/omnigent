import { expect, it } from "vitest";
import {
  feedbackRequest,
  feedbackReplyTargets,
  readFeedbackTarget,
  feedbackVisibleText,
} from "./feedbackPrompts";
import type { AnyBlock, BlockContext } from "./blocks";

const ctx: BlockContext = {
  agent: null,
  depth: 0,
  turn: 0,
  timestamp: 0,
  responseId: "",
  itemId: null,
};

const target = {
  responseId: "original",
  intent: "discuss" as const,
  original: { outcome: "partial", comment: "Needs verification", tags: ["Testing"] },
};
it("keeps the original target and feedback bound across a saved transcript", () => {
  const request = feedbackRequest("Review this", target, "Original answer");
  expect(readFeedbackTarget(request)).toEqual(target);
  expect(feedbackVisibleText(request)).toBe("Review this");
  expect(request).toContain("feedback-json");
  expect(request).toContain("without tools or project changes");
  const blocks: AnyBlock[] = [
    {
      type: "user_message",
      ctx: { ...ctx, itemId: "prompt" },
      content: [{ type: "input_text", text: request }],
    },
    {
      type: "text_done",
      ctx: { ...ctx, responseId: "reply" },
      fullText: "Suggestion",
      hasCodeBlocks: false,
    },
    {
      type: "user_message",
      ctx: { ...ctx, itemId: "next" },
      content: [{ type: "input_text", text: "Unrelated next task" }],
    },
    {
      type: "text_done",
      ctx: { ...ctx, responseId: "next-reply" },
      fullText: "Answer",
      hasCodeBlocks: false,
    },
  ];
  expect(feedbackReplyTargets(blocks).get("reply")).toEqual(target);
  expect(feedbackReplyTargets(blocks).has("next-reply")).toBe(false);
});
it("ignores malformed review context", () => {
  expect(readFeedbackTarget("hello")).toBeNull();
  expect(readFeedbackTarget("hello\n\nFeedback review context:\n{}")).toBeNull();
});
