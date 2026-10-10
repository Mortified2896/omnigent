import { expect, it } from "vitest";
import { parseFeedbackProposal } from "./FeedbackDiscussion";

it("accepts optional valid ratings and rejects invalid ratings", () => {
  const block = (outcome?: string) =>
    "```feedback-json\n" +
    JSON.stringify({ outcome, comment: "Needs verification", tags: ["Tools"] }) +
    "\n```";
  for (const outcome of ["success", "partial", "failed", "not_sure"] as const)
    expect(parseFeedbackProposal(block(outcome))?.outcome).toBe(outcome);
  expect(parseFeedbackProposal(block("perfect"))).toBeNull();
  expect(parseFeedbackProposal(block())).toEqual({
    comment: "Needs verification",
    tags: ["Tools"],
  });
});
