import { describe, expect, it } from "vitest";
import {
  groupModelOptions,
  groupedModelLabel,
  modelGroupLabel,
  stripModelLaneSuffix,
} from "./modelPickerGroups";
import type { NativeModelOption } from "./types";

function row(overrides: Partial<NativeModelOption> & { id: string }): NativeModelOption {
  return { displayName: overrides.id, ...overrides };
}

describe("groupModelOptions", () => {
  it("groups host-stamped access lanes in first-appearance order", () => {
    const groups = groupModelOptions([
      row({ id: "codex/gpt-6-astra", accessLane: "omniroute", groupLabel: "OmniRoute" }),
      row({ id: "codex/gpt-6.1-sol", accessLane: "codex-direct" }),
      row({ id: "codex/gpt-5.5", accessLane: "omniroute", groupLabel: "OmniRoute" }),
      row({ id: "glm-5.3", accessLane: "glm-direct", groupLabel: "GLM" }),
    ]);
    expect(groups.map((group) => group.key)).toEqual(["omniroute", "codex-direct", "glm-direct"]);
    expect(groups[0].options.map((option) => option.id)).toEqual([
      "codex/gpt-6-astra",
      "codex/gpt-5.5",
    ]);
    expect(groups[1].options.map((option) => option.id)).toEqual(["codex/gpt-6.1-sol"]);
  });

  it("keeps lane-less rows in one header-less group", () => {
    const groups = groupModelOptions([row({ id: "opus" }), row({ id: "sonnet" })]);
    expect(groups).toHaveLength(1);
    expect(groups[0].key).toBe("");
    expect(groups[0].label).toBeNull();
    expect(groups[0].options).toHaveLength(2);
  });

  it("labels GLM groups with the family and transport", () => {
    // GLM rides both the gateway and the direct lane; "GLM" alone would name
    // two indistinguishable groups.
    expect(
      modelGroupLabel(row({ id: "glm/glm-5.3", accessLane: "omniroute", groupLabel: "GLM" })),
    ).toBe("GLM · OmniRoute");
    expect(
      modelGroupLabel(row({ id: "glm-5.3", accessLane: "glm-direct", groupLabel: "GLM" })),
    ).toBe("GLM · Z.AI Direct");
    // The transport label alone when the family matches it.
    expect(modelGroupLabel(row({ id: "codex/gpt-5.5", accessLane: "omniroute" }))).toBe(
      "OmniRoute",
    );
    expect(modelGroupLabel(row({ id: "codex/gpt-5.5", accessLane: "codex-direct" }))).toBe(
      "Codex Subscription — Direct",
    );
    expect(modelGroupLabel(row({ id: "opus" }))).toBeNull();
  });
});

describe("stripModelLaneSuffix", () => {
  it("drops a lane suffix the heading already communicates", () => {
    const glm = row({ id: "glm/glm-5.3", accessLane: "omniroute", groupLabel: "GLM" });
    expect(stripModelLaneSuffix("GLM 4.5 · OmniRoute", glm)).toBe("GLM 4.5");
    expect(
      stripModelLaneSuffix(
        "GLM 5.3 Flash · Z.AI Direct",
        row({ id: "glm-5.3-flash", accessLane: "glm-direct", groupLabel: "GLM" }),
      ),
    ).toBe("GLM 5.3 Flash");
  });

  it("keeps a suffix that is not the row's own lane or family", () => {
    const codex = row({ id: "codex/gpt-5.5", accessLane: "codex-direct" });
    expect(stripModelLaneSuffix("GPT-5.5 Codex", codex)).toBe("GPT-5.5 Codex");
    expect(stripModelLaneSuffix("Model (fast tier)", codex)).toBe("Model (fast tier)");
  });

  it("leaves lane-less rows untouched", () => {
    expect(stripModelLaneSuffix("Sonnet 4.6 · something", row({ id: "sonnet" }))).toBe(
      "Sonnet 4.6 · something",
    );
  });
});

describe("groupedModelLabel", () => {
  it("renders the bare model label when the host baked a lane suffix in", () => {
    expect(
      groupedModelLabel(
        row({
          id: "codex/gpt-5.5",
          displayName: "GPT-5.5 · Codex Subscription — Direct",
          accessLane: "codex-direct",
        }),
        (option) => option.displayName ?? option.id,
      ),
    ).toBe("GPT-5.5");
  });

  it("keeps a genuine display name that merely contains a dot-separated part", () => {
    expect(
      groupedModelLabel(
        row({ id: "codex/gpt-5.5", displayName: "GPT-5.5", accessLane: "codex-direct" }),
        (option) => option.displayName ?? option.id,
      ),
    ).toBe("GPT-5.5");
  });
});
