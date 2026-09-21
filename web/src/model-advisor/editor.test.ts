import { describe, expect, it } from "vitest";
import {
  editorReducer,
  initialEditor,
  optionLabel,
  toggleCandidate,
  validateEditor,
  type AdvisorPreferences,
  type AdvisorOption,
} from "./editor";

const prefs: AdvisorPreferences = {
  schema_version: 1,
  enabled: true,
  allowed_candidate_ids: ["human"],
  advisor_candidate_id: "judge",
  human_probability_percent: 50,
};
const options: AdvisorOption[] = [
  {
    candidate_id: "human",
    model_id: "fixture-small",
    display_name: "Small",
    lane_id: "codex-direct",
    reasoning_effort: "low",
    available: true,
  },
  {
    candidate_id: "judge",
    model_id: "fixture-glm",
    display_name: "Advisor",
    lane_id: "glm-direct",
    reasoning_effort: "high",
    available: true,
  },
];
const saved = { version: 1, etag: '"v1"', preferences: prefs };
const hydrated = () =>
  editorReducer(initialEditor("owner:host"), { type: "hydrate", scope: "owner:host", saved });

describe("model advisor settings editor", () => {
  it("does not edit or save empty state before hydration", () => {
    const state = initialEditor("owner:host");
    expect(editorReducer(state, { type: "edit", preferences: prefs })).toBe(state);
  });
  it("hydrates saved choices rather than catalog defaults", () =>
    expect(hydrated().draft).toEqual(prefs));
  it("preserves unsaved changes across delayed hydration", () => {
    const edited = editorReducer(hydrated(), {
      type: "edit",
      preferences: { ...prefs, human_probability_percent: 30 },
    });
    expect(
      editorReducer(edited, { type: "hydrate", scope: "owner:host", saved }).draft
        ?.human_probability_percent,
    ).toBe(30);
  });
  it("ignores responses for another host", () =>
    expect(
      editorReducer(initialEditor("other"), { type: "hydrate", scope: "owner:host", saved }).draft,
    ).toBeNull());
  it("clears state when the owner/host scope changes", () =>
    expect(editorReducer(hydrated(), { type: "scope", scope: "other" }).draft).toBeNull());
  it("does not discard edits made while a save is pending", () => {
    const changed = { ...prefs, human_probability_percent: 30 };
    const edited = editorReducer(hydrated(), { type: "edit", preferences: changed });
    const state = editorReducer(edited, {
      type: "saved",
      scope: "owner:host",
      saved: { ...saved, version: 2 },
      submitted: prefs,
    });
    expect(state.draft).toEqual(changed);
    expect(state.dirty).toBe(true);
  });
  it("marks only the submitted draft saved", () => {
    const state = editorReducer(hydrated(), {
      type: "saved",
      scope: "owner:host",
      saved: { ...saved, version: 2 },
      submitted: prefs,
    });
    expect(state.dirty).toBe(false);
    expect(state.saved?.version).toBe(2);
  });
  it("rejects stale server versions", () => {
    const state = hydrated();
    expect(
      editorReducer(state, {
        type: "hydrate",
        scope: "owner:host",
        saved: { ...saved, version: 0 },
      }),
    ).toBe(state);
  });
  it("allows an advisor outside the answer pool", () =>
    expect(validateEditor(prefs, options)).toBeNull());
  it("blocks an unavailable saved choice instead of replacing it", () =>
    expect(validateEditor({ ...prefs, allowed_candidate_ids: ["missing"] }, options)).toContain(
      "unavailable",
    ));
  it("does not enroll newly discovered candidates", () =>
    expect(hydrated().draft?.allowed_candidate_ids).toEqual(["human"]));
  it("removes only the explicitly unchecked candidate", () =>
    expect(toggleCandidate(prefs, "human", false).allowed_candidate_ids).toEqual([]));
  it("keeps lane and effort visible", () => {
    expect(optionLabel(options[0])).toContain("ChatGPT plan · Direct");
    expect(optionLabel(options[1])).toContain("Z.AI Direct · high reasoning");
  });
});
