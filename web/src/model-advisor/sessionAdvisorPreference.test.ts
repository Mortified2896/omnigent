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
