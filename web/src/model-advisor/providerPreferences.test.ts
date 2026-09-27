import { describe, expect, it } from "vitest";

import {
  activeChoiceIds,
  effectiveOptions,
  normalizeProviderPreferences,
  toggleModel,
  toggleProvider,
  type LogicalOption,
  type ProviderPreferences,
  type ProviderPreferencesV2,
} from "./providerPreferences";

const TERRA_LOW: LogicalOption = {
  choice_id: "terra-low",
  provider: "openai",
  model_id: "gpt-5.6-terra",
  display_name: "GPT-5.6 Terra",
  reasoning_effort: "low",
  model_ids: ["codex/gpt-5.6-terra"],
  access_lanes: ["omniroute"],
  default_access_lanes: ["omniroute"],
  available: true,
};

const TERRA_HIGH: LogicalOption = {
  ...TERRA_LOW,
  choice_id: "terra-high",
  reasoning_effort: "high",
};

const LUNA_MEDIUM: LogicalOption = {
  choice_id: "luna-medium",
  provider: "openai",
  model_id: "gpt-5.6-luna",
  display_name: "GPT-5.6 Luna",
  reasoning_effort: "medium",
  model_ids: ["codex/gpt-5.6-luna"],
  access_lanes: ["omniroute"],
  available: true,
};

const GLM_HIGH: LogicalOption = {
  choice_id: "glm-high",
  provider: "glm",
  model_id: "glm-5.3",
  display_name: "GLM-5.3",
  reasoning_effort: "high",
  model_ids: ["glm-5.3"],
  access_lanes: ["glm-direct"],
  available: true,
};

const CATALOG = [TERRA_LOW, TERRA_HIGH, LUNA_MEDIUM, GLM_HIGH];

function preferences(): ProviderPreferences {
  return {
    schema_version: 3,
    enabled: true,
    providers: {
      openai: {
        enabled: true,
        collapsed: false,
        selected_choice_ids: [TERRA_LOW.choice_id, TERRA_HIGH.choice_id, LUNA_MEDIUM.choice_id],
        disabled_model_ids: [],
        transport_preference: "direct_only",
      },
      glm: {
        enabled: true,
        collapsed: true,
        selected_choice_ids: [GLM_HIGH.choice_id],
        disabled_model_ids: [],
        transport_preference: "omniroute_preferred",
      },
    },
    advisor_choice_id: TERRA_HIGH.choice_id,
    human_probability_percent: 35,
    unresolved_legacy_ids: [],
    route_review_required: [],
  };
}

describe("provider and model answer-pool masking", () => {
  it("turning a model off preserves its checked reasoning levels and restores them on", () => {
    const original = preferences();
    const off = toggleModel(original, "openai", TERRA_LOW.model_id, false);

    expect(off.providers.openai.selected_choice_ids).toEqual(
      original.providers.openai.selected_choice_ids,
    );
    expect(off.providers.openai.disabled_model_ids).toEqual([TERRA_LOW.model_id]);
    expect(effectiveOptions(off, CATALOG)).toEqual([LUNA_MEDIUM, GLM_HIGH]);
    expect(activeChoiceIds(off, CATALOG)).toEqual([LUNA_MEDIUM.choice_id, GLM_HIGH.choice_id]);
    expect(toggleModel(off, "openai", TERRA_LOW.model_id, true)).toEqual(original);
  });

  it("turning a provider off preserves model state, reasoning, and connection preference", () => {
    const original = toggleModel(preferences(), "openai", TERRA_LOW.model_id, false);
    const off = toggleProvider(original, "openai", false);

    expect(off.providers.openai).toEqual({
      ...original.providers.openai,
      enabled: false,
    });
    expect(effectiveOptions(off, CATALOG)).toEqual([GLM_HIGH]);
    expect(toggleProvider(off, "openai", true)).toEqual(original);
  });

  it("keeps the advisor eligible when its model is disabled from answers", () => {
    const off = toggleModel(preferences(), "openai", TERRA_LOW.model_id, false);
    expect(off.advisor_choice_id).toBe(TERRA_HIGH.choice_id);
    expect(
      effectiveOptions(off, CATALOG).some((choice) => choice.choice_id === off.advisor_choice_id),
    ).toBe(false);
  });

  it("does not silently include a selected reasoning level marked unavailable", () => {
    expect(() =>
      effectiveOptions(preferences(), [{ ...TERRA_LOW, available: false }, ...CATALOG.slice(1)]),
    ).toThrow(/unavailable from the live catalog/);
  });
});

describe("provider preference schema migration", () => {
  it("upgrades v2 without changing its existing selections or connection policy", () => {
    const current = preferences();
    const v2: ProviderPreferencesV2 = {
      schema_version: 2,
      enabled: true,
      providers: {
        openai: {
          enabled: false,
          collapsed: true,
          selected_choice_ids: [TERRA_LOW.choice_id, TERRA_HIGH.choice_id],
          transport_preference: "direct_only",
        },
        glm: {
          enabled: true,
          collapsed: false,
          selected_choice_ids: [GLM_HIGH.choice_id],
          transport_preference: "omniroute_preferred",
        },
      },
      advisor_choice_id: current.advisor_choice_id,
      human_probability_percent: current.human_probability_percent,
      unresolved_legacy_ids: ["legacy-unresolved"],
      route_review_required: ["glm"],
    };

    expect(normalizeProviderPreferences(v2)).toEqual({
      ...v2,
      schema_version: 3,
      providers: {
        openai: { ...v2.providers.openai, disabled_model_ids: [] },
        glm: { ...v2.providers.glm, disabled_model_ids: [] },
      },
    });
  });
});
