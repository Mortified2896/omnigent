const HISTORY_KEY = "o3-review-durations-v1";

export interface O3ReviewTiming {
  startedAt: number;
  estimatedMs: number;
  sampleCount: number;
  actualMs?: number;
}

function readDurations(): number[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(HISTORY_KEY) ?? "[]");
    return Array.isArray(value)
      ? value
          .filter((n): n is number => typeof n === "number" && Number.isFinite(n) && n > 0)
          .slice(-10)
      : [];
  } catch {
    return [];
  }
}

export function startReviewTiming(): O3ReviewTiming {
  const samples = readDurations().sort((a, b) => a - b);
  const middle = Math.floor(samples.length / 2);
  const median = samples.length % 2 ? samples[middle] : (samples[middle - 1] + samples[middle]) / 2;
  return {
    startedAt: performance.now(),
    estimatedMs: samples.length ? median : 60_000,
    sampleCount: samples.length,
  };
}

export function finishReviewTiming(timing: O3ReviewTiming): O3ReviewTiming {
  const actualMs = Math.max(1, performance.now() - timing.startedAt);
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify([...readDurations(), actualMs].slice(-10)));
  } catch {
    // Timing still works when browser storage is unavailable.
  }
  return { ...timing, actualMs };
}

export function reviewDuration(ms: number): string {
  return `${Math.round(Math.max(0, ms) / 1000)}s`;
}
