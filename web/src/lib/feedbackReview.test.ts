import { expect, it } from "vitest";
import {
  commentEdits,
  reviewedFeedback,
  ratingChangeKey,
  tagChangeKey,
  reviewChangeKeys,
} from "./feedbackReview";

it("groups replacements and preserves both versions exactly", () => {
  const before = "Needs live verification. 保留\n";
  const after = "Local checks pass; live verification. 保留\n";
  const edits = commentEdits(before, after);
  expect(edits.map((e) => e.before).join("")).toBe(before);
  expect(edits.map((e) => e.after).join("")).toBe(after);
  expect(edits.find((e) => e.before === "Needs")?.after).toBe("Local checks pass;");
  expect(new Set(edits.map((e) => e.key)).size).toBe(edits.length);
});
it("independently rejects one comment edit, rating and tag addition", () => {
  const original = { outcome: "partial", comment: "Needs live verification.", tags: ["Tests"] };
  const proposed = {
    outcome: "failed",
    comment: "Local live verification and deployment.",
    tags: ["Mobile"],
  };
  const replacement = commentEdits(original.comment, proposed.comment).find(
    (e) => e.before === "Needs",
  )!;
  const result = reviewedFeedback(original, proposed, {
    [replacement.key]: "rejected",
    [ratingChangeKey("partial", "failed")]: "rejected",
    [tagChangeKey("Mobile", true)]: "rejected",
    [tagChangeKey("Tests", false)]: "rejected",
  });
  expect(result).toEqual({
    outcome: "partial",
    comment: "Needs live verification and deployment.",
    tags: ["Tests"],
  });
});
it("all rejected changes reconstruct the original and all accepted reconstruct the proposal", () => {
  const original = { outcome: "partial", comment: "旧内容\n🙂  unchanged.", tags: ["Old"] };
  const proposed = { outcome: "success", comment: "新内容\n🙂  unchanged!", tags: ["New"] };
  const keys = reviewChangeKeys(original, proposed);
  expect(
    reviewedFeedback(original, proposed, Object.fromEntries(keys.map((k) => [k, "rejected"]))),
  ).toEqual(original);
  expect(
    reviewedFeedback(original, proposed, Object.fromEntries(keys.map((k) => [k, "accepted"]))),
  ).toEqual(proposed);
});
