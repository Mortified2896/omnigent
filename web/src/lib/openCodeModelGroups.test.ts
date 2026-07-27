import { describe, expect, it } from "vitest";

import type { HarnessModelOptionGroup } from "@/hooks/useHarnessModelOptions";
import {
  miniMaxProviderIndicator,
  openCodeModelMatches,
  openCodeOptionValue,
  organizeOpenCodeModelGroups,
} from "./openCodeModelGroups";

const model = (id: string, label: string) => ({ id, label, provider: "Provider" });

describe("organizeOpenCodeModelGroups", () => {
  it("classifies every model once from source metadata in the required order", () => {
    const genericCodexLabel = model("openai/gpt-5-codex", "GPT-5 Codex");
    const inputs: HarnessModelOptionGroup[] = [
      {
        source: "opencode-authenticated-catalog",
        models: [genericCodexLabel, model("google/gemini-3", "Gemini 3")],
      },
      {
        source: "opencode-minimax-token-plan-catalog",
        models: [
          {
            ...model("minimax-coding-plan/MiniMax-M3", "MiniMax M3"),
            access_source: "minimax-token-plan",
            availability: "available",
          },
        ],
      },
      {
        source: "omniroute",
        models: [model("coding-best", "Coding Best")],
      },
      {
        source: "opencode-codex-subscription-catalog",
        models: [
          {
            ...model("openai-codex/gpt-5.3-codex", "GPT-5.3 Codex"),
            access_source: "codex-subscription",
            availability: "available",
          },
        ],
      },
    ];

    const groups = organizeOpenCodeModelGroups(inputs);

    expect(groups.map(({ label }) => label)).toEqual([
      "Codex Subscription",
      "MiniMax Token Plan",
      "OmniRoute Combos",
      "Other OpenCode Models",
    ]);
    expect(groups.map(({ models }) => models.map(({ id }) => id))).toEqual([
      ["openai-codex/gpt-5.3-codex"],
      ["minimax-coding-plan/MiniMax-M3"],
      ["coding-best"],
      ["openai/gpt-5-codex", "google/gemini-3"],
    ]);
    expect(groups.flatMap(({ models }) => models)).toHaveLength(5);
    expect(new Set(groups.flatMap(({ models }) => models.map(({ id }) => id)))).toHaveProperty(
      "size",
      5,
    );
  });

  it("does not classify a generic Codex-like display label as a subscription model", () => {
    const groups = organizeOpenCodeModelGroups([
      {
        source: "opencode-authenticated-catalog",
        models: [model("provider/not-codex", "GPT-5 Codex")],
      },
    ]);

    expect(groups[0]?.models).toEqual([]);
    expect(groups[3]?.models.map(({ id }) => id)).toEqual(["provider/not-codex"]);
  });

  it("rejects unauthenticated plan entries even when their group source matches", () => {
    const groups = organizeOpenCodeModelGroups([
      {
        source: "opencode-codex-subscription-catalog",
        models: [
          {
            ...model("openai-codex/gpt", "GPT"),
            access_source: "codex-subscription",
            availability: "needs-auth",
          },
        ],
      },
      {
        source: "opencode-minimax-token-plan-catalog",
        models: [
          {
            ...model("minimax-coding-plan/MiniMax-M3", "MiniMax M3"),
            access_source: "wrong-source",
            availability: "available",
          },
        ],
      },
    ]);

    expect(groups[0]?.models).toEqual([]);
    expect(groups[1]?.models).toEqual([]);
  });

  it("keeps empty groups available for scoped unavailable states", () => {
    const groups = organizeOpenCodeModelGroups([]);

    expect(groups).toHaveLength(4);
    expect(groups.every(({ models }) => models.length === 0)).toBe(true);
  });

  it("deduplicates repeated wire IDs without rewriting them", () => {
    const duplicate = model("provider/model", "Model");
    const groups = organizeOpenCodeModelGroups([
      { source: "opencode-authenticated-catalog", models: [duplicate] },
      { source: "opencode-authenticated-catalog", models: [duplicate] },
    ]);

    expect(groups.flatMap(({ models }) => models).map(({ id }) => id)).toEqual(["provider/model"]);
  });
});

describe("OpenCode model presentation", () => {
  it("searches labels, full IDs, providers, and provider IDs", () => {
    const option = {
      ...model("minimax-cn-coding-plan/MiniMax-M3", "MiniMax M3"),
      provider: "MiniMax Token Plan",
      provider_id: "minimax-cn-coding-plan",
    };

    expect(openCodeModelMatches(option, "m3")).toBe(true);
    expect(openCodeModelMatches(option, "minimax-cn")).toBe(true);
    expect(openCodeModelMatches(option, "token plan")).toBe(true);
    expect(openCodeModelMatches(option, "gemini")).toBe(false);
  });

  it("preserves explicit OmniRoute route IDs", () => {
    expect(
      openCodeOptionValue(
        { key: "omniroute" },
        { ...model("display-id", "Route"), route_id: "coding-fast" },
      ),
    ).toBe("coding-fast");
    expect(openCodeOptionValue({ key: "other" }, model("provider/model", "Model"))).toBe(
      "provider/model",
    );
  });

  it("distinguishes MiniMax Global and China providers", () => {
    expect(miniMaxProviderIndicator("minimax-coding-plan")).toBe("Global");
    expect(miniMaxProviderIndicator("minimax-cn-coding-plan")).toBe("China");
  });
});
