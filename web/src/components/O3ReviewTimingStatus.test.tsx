import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { finishReviewTiming, startReviewTiming } from "@/lib/o3ReviewTiming";
import { O3ReviewTimingStatus } from "./O3ReviewTimingStatus";

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("review timing", () => {
  it("updates remaining time and explains an overrun without pretending completion", () => {
    vi.useFakeTimers();
    const now = vi.spyOn(performance, "now").mockReturnValue(0);
    render(<O3ReviewTimingStatus timing={startReviewTiming()} />);
    expect(screen.getByText(/Rough starting estimate/)).toBeTruthy();
    now.mockReturnValue(20_000);
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText(/20s elapsed · About 40s remaining/)).toBeTruthy();
    now.mockReturnValue(75_000);
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText(/15s over.*still waiting/)).toBeTruthy();
  });

  it("keeps the original estimate for comparison and learns from completed reviews", () => {
    const now = vi.spyOn(performance, "now").mockReturnValue(0);
    const start = startReviewTiming();
    now.mockReturnValue(90_000);
    const finished = finishReviewTiming(start);
    render(<O3ReviewTimingStatus timing={finished} />);
    expect(screen.getByRole("status").textContent).toContain(
      "Review received in 90s · Estimated 60s · 30s slower than estimated (50%)",
    );
    expect(startReviewTiming()).toMatchObject({ estimatedMs: 90_000, sampleCount: 1 });
  });

  it("ignores invalid stored data and tolerates unavailable storage", () => {
    localStorage.setItem("o3-review-durations-v1", '[null,-1,0,"bad",10000,30000]');
    expect(startReviewTiming()).toMatchObject({ estimatedMs: 20_000, sampleCount: 2 });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => finishReviewTiming(startReviewTiming())).not.toThrow();
  });
});
