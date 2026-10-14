import { beforeEach, expect, it, vi } from "vitest";
import { readKeepChosenModel, writeKeepChosenModel } from "./sessionAdvisorPreference";

vi.mock("@/lib/identity", () => ({ getCurrentAuthorId: () => "test" }));
beforeEach(() => localStorage.clear());
it("allows model changes by default and preserves an explicit model pin", () => {
  expect(readKeepChosenModel("host", "chat")).toBe(false);
  writeKeepChosenModel("host", "chat", true);
  expect(readKeepChosenModel("host", "chat")).toBe(true);
  writeKeepChosenModel("host", "chat", false);
  expect(readKeepChosenModel("host", "chat")).toBe(false);
});

it("restores Jev choices and the selected Claude pool after navigation", async () => {
  const { readSessionAdvisorChoices, writeSessionAdvisorChoices } =
    await import("./sessionAdvisorPreference");
  const { emptyProviderPreferences, providerSelection } = await import("./providerPreferences");
  const preferences = emptyProviderPreferences();
  preferences.advisor_model_mode = "jev";
  preferences.providers.anthropic = {
    ...providerSelection(preferences, "anthropic"),
    enabled: true,
    selected_choice_ids: ["claude-choice"],
  };
  const choices = {
    preferences,
    humanPick: { model: "claude-test", accessLane: "claude-direct", effort: "high" },
  };
  writeSessionAdvisorChoices("host", "chat", choices);
  expect(readSessionAdvisorChoices("host", "chat")).toEqual(choices);
});
