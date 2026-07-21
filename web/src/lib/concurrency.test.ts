import { describe, expect, it } from "vitest";
import { runWithConcurrency } from "./concurrency";

describe("runWithConcurrency", () => {
  it("runs all items and preserves per-index order", async () => {
    const out = await runWithConcurrency([1, 2, 3], async (n) => n * 2, 4);
    expect(out.map((r) => r.value)).toEqual([2, 4, 6]);
  });
  it("captures per-index errors", async () => {
    const out = await runWithConcurrency(
      [1, 2, 3],
      async (n) => {
        if (n === 2) throw new Error("boom");
        return n;
      },
      4,
    );
    expect(out[0].value).toBe(1);
    expect(out[1].error).toBeInstanceOf(Error);
    expect(out[2].value).toBe(3);
  });
});