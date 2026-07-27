import { describe, expect, it } from "vitest";
import {
  CURATED_OMNIROUTE_COMBO_IDS,
  OMNIROUTE_COMBO_DISPLAY_NAMES,
  findOmniRouteCombo,
  getOmniRouteComboDisplayName,
  isCuratedOmniRouteCombo,
} from "./omnirouteCombos";

describe("OMNIROUTE_COMBO_DISPLAY_NAMES", () => {
  it("has the three curated combos with friendly names", () => {
    expect(OMNIROUTE_COMBO_DISPLAY_NAMES["auto/best-coding"]).toBe("OmniRoute Coding Best");
    expect(OMNIROUTE_COMBO_DISPLAY_NAMES["auto/coding:fast"]).toBe("OmniRoute Coding Fast");
    expect(OMNIROUTE_COMBO_DISPLAY_NAMES["auto/coding:reliable"]).toBe("OmniRoute Coding Reliable");
  });

  it("preserves colon and slash characters in the curated ids", () => {
    const ids = Object.keys(OMNIROUTE_COMBO_DISPLAY_NAMES);
    expect(ids).toContain("auto/best-coding");
    expect(ids).toContain("auto/coding:fast");
    expect(ids).toContain("auto/coding:reliable");
  });

  it("all curated names start with 'OmniRoute' for visual grouping", () => {
    for (const name of Object.values(OMNIROUTE_COMBO_DISPLAY_NAMES)) {
      expect(name.startsWith("OmniRoute")).toBe(true);
    }
  });
});

describe("CURATED_OMNIROUTE_COMBO_IDS", () => {
  it("contains exactly the three curated combos in the expected order", () => {
    expect(CURATED_OMNIROUTE_COMBO_IDS).toEqual([
      "auto/best-coding",
      "auto/coding:fast",
      "auto/coding:reliable",
    ]);
  });
});

describe("getOmniRouteComboDisplayName", () => {
  it("returns the curated display name when the id is known", () => {
    expect(getOmniRouteComboDisplayName("auto/best-coding")).toBe("OmniRoute Coding Best");
    expect(getOmniRouteComboDisplayName("auto/coding:fast")).toBe("OmniRoute Coding Fast");
    expect(getOmniRouteComboDisplayName("auto/coding:reliable")).toBe("OmniRoute Coding Reliable");
  });

  it("falls back to the raw id for unknown combos (preserving colons + slashes)", () => {
    expect(getOmniRouteComboDisplayName("auto/some-future")).toBe("auto/some-future");
    expect(getOmniRouteComboDisplayName("auto/some:future")).toBe("auto/some:future");
  });

  it("returns an empty string for null / undefined / empty ids", () => {
    expect(getOmniRouteComboDisplayName(null)).toBe("");
    expect(getOmniRouteComboDisplayName(undefined)).toBe("");
    expect(getOmniRouteComboDisplayName("")).toBe("");
  });
});

describe("isCuratedOmniRouteCombo", () => {
  it("returns true for the three curated ids", () => {
    expect(isCuratedOmniRouteCombo("auto/best-coding")).toBe(true);
    expect(isCuratedOmniRouteCombo("auto/coding:fast")).toBe(true);
    expect(isCuratedOmniRouteCombo("auto/coding:reliable")).toBe(true);
  });

  it("returns false for non-curated ids (including concrete models)", () => {
    expect(isCuratedOmniRouteCombo("auto/coding")).toBe(false);
    expect(isCuratedOmniRouteCombo("gpt-5.5")).toBe(false);
    expect(isCuratedOmniRouteCombo("databricks-gpt-5-5")).toBe(false);
  });

  it("returns false for null / undefined", () => {
    expect(isCuratedOmniRouteCombo(null)).toBe(false);
    expect(isCuratedOmniRouteCombo(undefined)).toBe(false);
  });
});

describe("findOmniRouteCombo", () => {
  const catalog = [
    {
      id: "auto/best-coding",
      display_name: "OmniRoute Coding Best",
      provider: "omniroute" as const,
      kind: "combo" as const,
      reasoning_efforts: ["medium", "high"],
      max_reasoning_effort: "high",
      default_reasoning_effort: "medium",
      requires_explicit_approval: false,
    },
    {
      id: "auto/coding:fast",
      display_name: "OmniRoute Coding Fast",
      provider: "omniroute" as const,
      kind: "combo" as const,
      reasoning_efforts: ["low", "medium"],
      max_reasoning_effort: "medium",
      default_reasoning_effort: "low",
      requires_explicit_approval: false,
    },
  ];

  it("returns the matching entry by id", () => {
    const found = findOmniRouteCombo(catalog, "auto/coding:fast");
    expect(found?.display_name).toBe("OmniRoute Coding Fast");
  });

  it("returns null when the catalog is missing or empty", () => {
    expect(findOmniRouteCombo(null, "auto/best-coding")).toBeNull();
    expect(findOmniRouteCombo([], "auto/best-coding")).toBeNull();
  });

  it("returns null when the id is not present in the catalog", () => {
    expect(findOmniRouteCombo(catalog, "auto/missing")).toBeNull();
  });

  it("returns null for null / empty id", () => {
    expect(findOmniRouteCombo(catalog, null)).toBeNull();
    expect(findOmniRouteCombo(catalog, "")).toBeNull();
  });
});
