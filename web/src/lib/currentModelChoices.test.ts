import { expect, it } from "vitest";
import { currentModelChoices } from "./currentModelChoices";

it("keeps only the newest advertised GPT-6 family without inventing upgrades", () => {
  const rows = ["gpt-5.6-sol", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "glm-5.3"];
  expect(currentModelChoices(rows, (id) => id)).toEqual([
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-luna",
    "glm-5.3",
  ]);
});
it("applies the same model filtering to direct and routed model names", () => {
  const rows = ["codex/gpt-5.6-sol", "codex/gpt-6-sol", "gpt-6.1-sol", "openai/gpt-6.1-sol"];
  expect(currentModelChoices(rows, (id) => id)).toEqual(["gpt-6.1-sol", "openai/gpt-6.1-sol"]);
});
