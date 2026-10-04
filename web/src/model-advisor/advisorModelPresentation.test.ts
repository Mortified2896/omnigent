import { describe, expect, it } from "vitest";

import type { LogicalOption } from "@/model-advisor/providerPreferences";
import {
  advisorEffortOptions,
  advisorGroupHeading,
  advisorModelKey,
  advisorModelRows,
  reconcileAdvisorChoice,
  seedFreshAdvisorDraft,
} from "@/model-advisor/advisorModelPresentation";
import { emptyProviderPreferences } from "@/model-advisor/providerPreferences";

function logical(overrides: Partial<LogicalOption> & { choice_id: string }): LogicalOption {
  return {
    provider: "openai",
    model_id: "gpt-5.5",
    display_name: "GPT-5.5",
    reasoning_effort: "medium",
    model_ids: ["gpt-5.5"],
    access_lanes: ["codex-direct", "omniroute"],
    default_access_lanes: [],
    available: true,
    ...overrides,
  };
}

describe("advisorModelRows", () => {
  it("collapses efforts into one row per provider/checkpoint and shows every lane as the route line", () => {
    const rows = advisorModelRows([
      logical({ choice_id: "a-low", reasoning_effort: "low", default_access_lanes: ["codex-direct"] }),
      logical({ choice_id: "a-high", reasoning_effort: "high" }),
      logical({
        choice_id: "g-max",
        provider: "glm",
        model_id: "glm-5.3",
        display_name: "GLM 5.3",
        reasoning_effort: "max",
        model_ids: ["glm/glm-5.3"],
        access_lanes: ["glm-direct", "omniroute"],
      }),
    ]);
    expect(rows).toHaveLength(2);
    const openai = rows[0];
    expect(openai.key).toBe(advisorModelKey({ provider: "openai", model_id: "gpt-5.5" }));
    expect(openai.displayName).toBe("GPT-5.5");
    expect(openai.available).toBe(true);
    // Shared route vocabulary with the primary picker: host lane labels.
    expect(openai.routeSummary).toBe("Codex Subscription — Direct · OmniRoute");
    expect(openai.choices.map((choice) => choice.reasoning_effort)).toEqual(["low", "high"]);
    const glm = rows[1];
    expect(glm.routeSummary).toBe("Z.AI Direct · OmniRoute");
    expect(glm.keywords).toEqual(
      expect.arrayContaining(["GLM 5.3", "glm/glm-5.3", "Z.AI Direct", "OmniRoute"]),
    );
  });

  it("marks a row unavailable only when no effort choice is qualified", () => {
    const rows = advisorModelRows([
      logical({ choice_id: "a-low", available: false, unavailable_reason: "probe failed" }),
      logical({ choice_id: "a-high", available: true }),
      logical({
        choice_id: "b-low",
        provider: "glm",
        model_id: "glm-5.3",
        display_name: "GLM 5.3",
        reasoning_effort: "low",
        access_lanes: ["glm-direct"],
        available: false,
        unavailable_reason: "z.ai lane offline",
      }),
    ]);
    expect(rows[0].available).toBe(true);
    expect(rows[0].disabledReason).toBeUndefined();
    expect(rows[1].available).toBe(false);
    expect(rows[1].disabledReason).toBe("z.ai lane offline");
    expect(rows[1].routeSummary).toBe("");
  });
});

describe("advisorGroupHeading / advisorEffortOptions", () => {
  it("uses the provider+plan vocabulary for group headings", () => {
    expect(advisorGroupHeading("openai")).toBe("OpenAI / ChatGPT plan");
    expect(advisorGroupHeading("glm")).toBe("GLM / Z.AI");
  });

  it("offers only available choices, ascending effort rank", () => {
    const choices = [
      logical({ choice_id: "max", reasoning_effort: "max" }),
      logical({ choice_id: "low", reasoning_effort: "low", available: false }),
      logical({ choice_id: "high", reasoning_effort: "high" }),
    ];
    expect(advisorEffortOptions(choices).map((option) => option.value)).toEqual(["high", "max"]);
  });
});

describe("reconcileAdvisorChoice", () => {
  const choices = [
    logical({ choice_id: "low", reasoning_effort: "low" }),
    logical({ choice_id: "high", reasoning_effort: "high", default_access_lanes: ["codex-direct"] }),
    logical({ choice_id: "max", reasoning_effort: "max" }),
  ];

  it("keeps the current effort when the new model still offers it", () => {
    expect(reconcileAdvisorChoice(choices, "max")?.choice_id).toBe("max");
  });

  it("falls back to the catalog-declared default effort", () => {
    expect(reconcileAdvisorChoice(choices, "xhigh")?.choice_id).toBe("high");
  });

  it("falls back to the deterministic lowest supported effort", () => {
    expect(
      reconcileAdvisorChoice(
        [logical({ choice_id: "b", reasoning_effort: "max" }), logical({ choice_id: "a", reasoning_effort: "high" })],
        null,
      )?.choice_id,
    ).toBe("a");
  });

  it("returns null only when nothing is available", () => {
    expect(
      reconcileAdvisorChoice([logical({ choice_id: "x", available: false })], "low"),
    ).toBeNull();
  });

  it("treats not_applicable as a real choice, never an implicit default", () => {
    const noEffortOnly = [logical({ choice_id: "na", reasoning_effort: "not_applicable" })];
    // Kept when it is the current effort, and the deterministic fallback for a
    // model that only advertises "no effort setting".
    expect(reconcileAdvisorChoice(noEffortOnly, "not_applicable")?.choice_id).toBe("na");
    expect(reconcileAdvisorChoice(noEffortOnly, null)?.choice_id).toBe("na");
  });
});

describe("seedFreshAdvisorDraft", () => {
  it("seeds an empty draft with declared defaults so enabling is valid immediately", () => {
    const draft = { ...emptyProviderPreferences(), enabled: true };
    const options = [
      logical({
        choice_id: "openai-default",
        reasoning_effort: "medium",
        default_access_lanes: ["codex-direct"],
      }),
      logical({ choice_id: "openai-other", reasoning_effort: "high" }),
      logical({
        choice_id: "glm-default",
        provider: "glm",
        model_id: "glm-5.3",
        display_name: "GLM 5.3",
        reasoning_effort: "max",
        access_lanes: ["glm-direct"],
        default_access_lanes: ["glm-direct"],
      }),
    ];
    const seeded = seedFreshAdvisorDraft(draft, options, null);
    expect(seeded).not.toBeNull();
    expect(seeded?.advisor_choice_id).toBe("openai-default");
    expect(seeded?.providers.openai.selected_choice_ids).toContain("openai-default");
    expect(seeded?.providers.glm.selected_choice_ids).toEqual(["glm-default"]);
  });

  it("keeps the composer's human choice in the seeded pool", () => {
    const draft = { ...emptyProviderPreferences(), enabled: true };
    const options = [
      logical({ choice_id: "human-pick", reasoning_effort: "high" }),
      logical({
        choice_id: "glm-default",
        provider: "glm",
        model_id: "glm-5.3",
        display_name: "GLM 5.3",
        reasoning_effort: "low",
        access_lanes: ["glm-direct"],
      }),
    ];
    const seeded = seedFreshAdvisorDraft(draft, options, "human-pick");
    expect(seeded?.providers.openai.selected_choice_ids).toContain("human-pick");
    expect(seeded?.providers.glm.selected_choice_ids).toEqual(["glm-default"]);
    // The recommender lands on the deterministic declared default across the
    // seeded pool (lowest effort rank): GLM's low here.
    expect(seeded?.advisor_choice_id).toBe("glm-default");
  });

  it("returns null when a never-configured draft faces an empty catalog", () => {
    const draft = { ...emptyProviderPreferences(), enabled: true };
    const options = [logical({ choice_id: "x", available: false })];
    expect(seedFreshAdvisorDraft(draft, options, null)).toBeNull();
  });

  it("never touches a configured draft or its remembered pool", () => {
    const draft = {
      ...emptyProviderPreferences(),
      enabled: true,
      providers: {
        ...emptyProviderPreferences().providers,
        openai: {
          ...emptyProviderPreferences().providers.openai,
          selected_choice_ids: ["remembered"],
        },
      },
      advisor_choice_id: "remembered",
    };
    const options = [
      logical({ choice_id: "fresh-default", reasoning_effort: "low", default_access_lanes: ["codex-direct"] }),
    ];
    expect(seedFreshAdvisorDraft(draft, options, null)).toBe(draft);
  });

  it("seeds only the recommender when a saved pool exists without an advisor choice", () => {
    const draft = {
      ...emptyProviderPreferences(),
      enabled: true,
      providers: {
        ...emptyProviderPreferences().providers,
        glm: {
          ...emptyProviderPreferences().providers.glm,
          selected_choice_ids: ["glm-saved"],
        },
      },
    };
    const options = [
      logical({ choice_id: "glm-saved", provider: "glm", model_id: "glm-5.3", display_name: "GLM 5.3", reasoning_effort: "high", access_lanes: ["glm-direct"] }),
    ];
    const seeded = seedFreshAdvisorDraft(draft, options, null);
    expect(seeded?.providers.glm.selected_choice_ids).toEqual(["glm-saved"]);
    expect(seeded?.advisor_choice_id).toBe("glm-saved");
  });

  it("leaves migration-review drafts alone", () => {
    const draft = {
      ...emptyProviderPreferences(),
      enabled: true,
      unresolved_legacy_ids: ["legacy-1"],
    };
    expect(seedFreshAdvisorDraft(draft, [logical({ choice_id: "a" })], null)).toBe(draft);
  });
});
