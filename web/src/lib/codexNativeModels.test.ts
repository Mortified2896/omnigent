import { describe, expect, it } from "vitest";

import {
  codexEffortLadderForModel,
  codexEffortLevelsForModel,
  findNativeModelOption,
  isCodexNativeModel,
  reconcileCodexEffortForModel,
} from "./codexNativeModels";
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

describe("Codex native catalog inclusion and lane stamping", () => {
  // Rows as the host's per-lane probe stamps them: verbatim native ids, the
  // lane metadata added around them, per-model reasoning efforts preserved.
  const directLaneRows: NativeModelOption[] = [
    {
      id: "gpt-6.1-sol",
      model: "gpt-6.1-sol",
      displayName: "GPT-6.1-Sol",
      accessLane: "codex-direct",
      groupLabel: "Codex Subscription — Direct",
      isDefault: true,
      defaultReasoningEffort: "low",
      supportedReasoningEfforts: [
        { reasoningEffort: "low" },
        { reasoningEffort: "medium" },
        { reasoningEffort: "high" },
        { reasoningEffort: "xhigh" },
        { reasoningEffort: "max" },
        { reasoningEffort: "ultra" },
      ],
    },
    {
      id: "gpt-5.5",
      model: "gpt-5.5",
      displayName: "GPT-5.5",
      accessLane: "codex-direct",
      groupLabel: "Codex Subscription — Direct",
      supportedReasoningEfforts: [
        { reasoningEffort: "low" },
        { reasoningEffort: "medium" },
        { reasoningEffort: "high" },
        { reasoningEffort: "xhigh" },
      ],
    },
  ];

  it("offers a model the live account advertises, identified natively", () => {
    // A model only the CURRENT runtime advertises must be pickable by its
    // native id — the account truth, not a hardcoded catalog entry.
    expect(findNativeModelOption(directLaneRows, "gpt-6.1-sol")).toBe(directLaneRows[0]);
    expect(isCodexNativeModel(directLaneRows, "gpt-6.1-sol")).toBe(true);
  });

  it("resolves a legacy catalog-qualified id onto the native row", () => {
    // A sticky session id spelled the gateway way must still resolve to the
    // native row of the same model (the codex/ prefix folds away).
    expect(findNativeModelOption(directLaneRows, "codex/gpt-5.5")).toBe(directLaneRows[1]);
  });

  it("preserves the selected model's own effort ladder across lanes", () => {
    // GPT-6.1-Sol advertises low→ultra while GPT-5.5 stops at xhigh: the
    // per-model ladders must survive lane stamping and stay distinct.
    expect(codexEffortLevelsForModel(directLaneRows, "gpt-6.1-sol")).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
      "ultra",
    ]);
    expect(codexEffortLevelsForModel(directLaneRows, "gpt-5.5")).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
    ]);
    // An unknown model exposes no ladder rather than another model's.
    expect(codexEffortLevelsForModel(directLaneRows, "gpt-6.9-ghost")).toEqual([]);
  });
});

describe("Codex effort ladder baseline for unadvertised rows", () => {
  const glmOmniRouteRows: NativeModelOption[] = [
    {
      // The gateway serves this GLM route without effort_tiers — the exact
      // live shape that hid the reasoning picker before the baseline existed.
      id: "glm/glm-5-turbo",
      model: "glm/glm-5-turbo",
      displayName: "GLM 5 Turbo · OmniRoute",
      accessLane: "omniroute",
      groupLabel: "GLM",
    },
    {
      id: "glm/glm-5.3",
      model: "glm/glm-5.3",
      displayName: "GLM 5.3 · OmniRoute",
      accessLane: "omniroute",
      groupLabel: "GLM",
      supportedReasoningEfforts: [
        { reasoningEffort: "low" },
        { reasoningEffort: "high" },
        { reasoningEffort: "max" },
      ],
    },
  ];

  it("falls back to the baseline rungs for a resolved row without tiers", () => {
    expect(codexEffortLadderForModel(glmOmniRouteRows, "glm/glm-5-turbo")).toEqual([
      "low",
      "medium",
      "high",
    ]);
  });

  it("keeps a row's advertised ladder untouched", () => {
    expect(codexEffortLadderForModel(glmOmniRouteRows, "glm/glm-5.3")).toEqual([
      "low",
      "high",
      "max",
    ]);
  });

  it("still hides the ladder for an unresolved model", () => {
    expect(codexEffortLadderForModel(glmOmniRouteRows, "glm/glm-9-ghost")).toEqual([]);
  });

  it("keeps the strict variant empty for unadvertised rows (Devin semantics)", () => {
    expect(codexEffortLevelsForModel(glmOmniRouteRows, "glm/glm-5-turbo")).toEqual([]);
  });
});

describe("reconcileCodexEffortForModel", () => {
  const rows: NativeModelOption[] = [
    {
      id: "gpt-5.6-sol",
      model: "gpt-5.6-sol",
      displayName: "GPT-5.6-Sol",
      defaultReasoningEffort: "low",
      supportedReasoningEfforts: [
        { reasoningEffort: "low" },
        { reasoningEffort: "medium" },
        { reasoningEffort: "high" },
      ],
    },
    {
      id: "gpt-5.6-terra",
      model: "gpt-5.6-terra",
      displayName: "GPT-5.6-Terra",
      defaultReasoningEffort: "medium",
      supportedReasoningEfforts: [
        { reasoningEffort: "low" },
        { reasoningEffort: "medium" },
        { reasoningEffort: "high" },
      ],
    },
    {
      id: "glm/glm-5-turbo",
      model: "glm/glm-5-turbo",
      displayName: "GLM 5 Turbo · OmniRoute",
      accessLane: "omniroute",
      groupLabel: "GLM",
    },
  ];

  it("keeps the persisted effort when the new model still supports it", () => {
    expect(reconcileCodexEffortForModel(rows, "gpt-5.6-terra", "high")).toBe("high");
  });

  it("lands on the new model's catalog-declared default otherwise", () => {
    expect(reconcileCodexEffortForModel(rows, "gpt-5.6-terra", "max")).toBe("medium");
  });

  it("lands on the declared default when the previous effort was empty", () => {
    expect(reconcileCodexEffortForModel(rows, "gpt-5.6-sol", "")).toBe("low");
  });

  it("falls back to the deterministic middle rung when nothing is declared", () => {
    expect(reconcileCodexEffortForModel(rows, "glm/glm-5-turbo", "xhigh")).toBe("medium");
  });

  it("returns no effort when the model resolves to no ladder", () => {
    expect(reconcileCodexEffortForModel(rows, "unknown-model", "high")).toBe("");
    expect(reconcileCodexEffortForModel(rows, null, "high")).toBe("");
  });
});
