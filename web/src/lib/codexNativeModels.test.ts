import { describe, expect, it } from "vitest";

import { codexEffortLevelsForModel, findNativeModelOption } from "./codexNativeModels";
import type { NativeModelOption } from "./types";

const qualifiedCodexRow: NativeModelOption = {
  id: "codex/gpt-5.5",
  model: "codex/gpt-5.5",
  displayName: "GPT-5.5",
  supportedReasoningEfforts: [
    { reasoningEffort: "low" },
    { reasoningEffort: "medium" },
    { reasoningEffort: "high" },
    { reasoningEffort: "max" },
  ],
};

describe("Codex native model matching", () => {
  it("matches an active bare Codex model to a codex-qualified catalog row", () => {
    expect(findNativeModelOption([qualifiedCodexRow], "gpt-5.5")).toBe(qualifiedCodexRow);
  });

  it("keeps active-chat reasoning options visible for a codex-qualified catalog row", () => {
    expect(codexEffortLevelsForModel([qualifiedCodexRow], "gpt-5.5")).toEqual([
      "low",
      "medium",
      "high",
      "max",
    ]);
  });
});
