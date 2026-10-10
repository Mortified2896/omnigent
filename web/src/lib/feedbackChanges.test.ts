import { expect, it } from "vitest";
import { diffFeedbackText } from "./feedbackChanges";

it("marks changed words while keeping the surrounding sentence unchanged", () => {
  expect(diffFeedbackText("Local checks passed.", "Live checks passed.")).toEqual([
    { kind: "removed", text: "Local" },
    { kind: "added", text: "Live" },
    { kind: "unchanged", text: " checks passed." },
  ]);
});

it.each([
  ["", ""],
  ["", "Add a comment."],
  ["Remove this comment.", ""],
  ["No changes.", "No changes."],
  [
    "Needs live verification.\n\nKeep this note.",
    "Local checks passed. Live verification is pending.\n\nKeep this note.",
  ],
  ["Keep  two spaces\nand a new line.", "Keep one space\n\nand another new line."],
  ["这个结果完全成功 ✅", "这个结果部分成功 ✅，还要验证。"],
  ["Passed, pending verification.", "Passed; pending verification!"],
])("preserves both versions exactly: %j to %j", (before, after) => {
  const changes = diffFeedbackText(before, after);
  expect(
    changes
      .filter((part) => part.kind !== "added")
      .map((part) => part.text)
      .join(""),
  ).toBe(before);
  expect(
    changes
      .filter((part) => part.kind !== "removed")
      .map((part) => part.text)
      .join(""),
  ).toBe(after);
  expect(changes.every((part) => part.text.length > 0)).toBe(true);
});

it("keeps large unrelated comments compact while retaining a shared ending", () => {
  const before = "alpha ".repeat(650) + "\nKeep this ending.";
  const after = "beta ".repeat(780) + "\nKeep this ending.";
  const changes = diffFeedbackText(before, after);
  expect(changes.length).toBeLessThanOrEqual(4);
  expect(changes.at(-1)).toEqual({ kind: "unchanged", text: " \nKeep this ending." });
  expect(
    changes
      .filter((part) => part.kind !== "added")
      .map((part) => part.text)
      .join(""),
  ).toBe(before);
  expect(
    changes
      .filter((part) => part.kind !== "removed")
      .map((part) => part.text)
      .join(""),
  ).toBe(after);
});
