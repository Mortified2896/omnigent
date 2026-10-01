import { describe, expect, it } from "vitest";
import { codexPlanModeFromLabels } from "./codexPlanMode";

describe("Codex collaboration mode provenance", () => {
  it("prefers the canonical label, including an explicit clear", () => {
    const legacy = { "omnigent.codex_native.collaboration_mode": "plan" };
    expect(codexPlanModeFromLabels(legacy)).toBe(true);
    expect(
      codexPlanModeFromLabels({
        ...legacy,
        "omnigent.codex_native.collaboration_mode": null,
      }),
    ).toBe(false);
    expect(
      codexPlanModeFromLabels({
        "omnigent.codex_native.collaboration_mode": "plan",
      }),
    ).toBe(true);
  });
});
