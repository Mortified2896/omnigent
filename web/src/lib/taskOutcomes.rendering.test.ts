import { describe, expect, it } from "vitest";
import type { Bubble } from "./renderItems";
import { canonicalOutcomeResponseIds, ownsOutcomeCard } from "./taskOutcomes";

function assistant(responseId: string, lifecycle: "completed" | "streaming" = "completed"): Bubble {
  return {
    kind: "assistant",
    responseId,
    stableId: `${responseId}-${lifecycle}`,
    lifecycle,
    error: null,
    items: [],
  };
}

describe("task outcome rendering", () => {
  it("renders a review shared by live and hydrated state once", () => {
    const bubbles = [assistant("run-1"), assistant("run-1")];
    expect(canonicalOutcomeResponseIds(bubbles)).toEqual(new Set(["run-1"]));
    expect(ownsOutcomeCard(bubbles, 0)).toBe(false);
    expect(ownsOutcomeCard(bubbles, 1)).toBe(true);
  });

  it("renders two distinct review IDs twice", () => {
    const bubbles = [assistant("run-1"), assistant("run-2")];
    expect(canonicalOutcomeResponseIds(bubbles)).toEqual(new Set(["run-1", "run-2"]));
    expect(ownsOutcomeCard(bubbles, 0)).toBe(true);
    expect(ownsOutcomeCard(bubbles, 1)).toBe(true);
  });

  it("keeps the canonical owner stable across hydration/rerender", () => {
    const hydrated = [assistant("run-1"), assistant("run-2"), assistant("run-1")];
    expect(hydrated.filter((_, index) => ownsOutcomeCard(hydrated, index))).toHaveLength(2);
    expect(ownsOutcomeCard(hydrated, 2)).toBe(true);
    expect(ownsOutcomeCard(hydrated, 0)).toBe(false);
  });
});
