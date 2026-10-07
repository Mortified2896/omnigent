import { getCurrentUserId } from "@/lib/identity";

export interface PromptTiming {
  startedAt: number;
  estimatedSeconds: number;
  sampleCount: number;
}
const pending = new Map<string, PromptTiming>();
function historyKey() {
  return `omnigent.prompt-times:${getCurrentUserId()}:v1`;
}
function pendingKey(sessionId: string) {
  return `omnigent.prompt-timing:${getCurrentUserId()}:${sessionId}`;
}
function durations(): number[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(historyKey()) ?? "[]");
    return Array.isArray(value)
      ? value
          .filter(
            (n): n is number => typeof n === "number" && Number.isFinite(n) && n > 0 && n < 86400,
          )
          .slice(-20)
      : [];
  } catch {
    return [];
  }
}
export function estimateDuration(samples: readonly number[]): number {
  if (!samples.length) return 60;
  const sorted = [...samples].sort((a, b) => a - b);
  return Math.max(1, sorted[Math.floor((sorted.length - 1) * 0.75)]!);
}
export function startPromptTiming(startedAt = Date.now(), fallback: number[] = []): PromptTiming {
  const history = durations();
  const samples = (history.length ? history : fallback).slice().sort((a, b) => a - b);
  return {
    startedAt,
    estimatedSeconds: estimateDuration(samples),
    sampleCount: samples.length,
  };
}
export function recordPromptDuration(seconds: number) {
  try {
    localStorage.setItem(historyKey(), JSON.stringify([...durations(), seconds].slice(-20)));
  } catch {
    /* Storage is optional. */
  }
}
/** Carry the Send timestamp and fixed estimate across Advisor → chat navigation. */
export function handoffPromptTiming(sessionId: string, timing: PromptTiming) {
  pending.set(pendingKey(sessionId), timing);
  try {
    sessionStorage.setItem(pendingKey(sessionId), JSON.stringify(timing));
  } catch {
    /* In-memory handoff still works. */
  }
}
export function readPromptTiming(sessionId: string): PromptTiming | null {
  const key = pendingKey(sessionId);
  const cached = pending.get(key);
  if (cached) return cached;
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null") as PromptTiming | null;
    if (
      value &&
      Number.isFinite(value.startedAt) &&
      value.startedAt <= Date.now() &&
      Date.now() - value.startedAt < 86400000 &&
      Number.isFinite(value.estimatedSeconds) &&
      value.estimatedSeconds > 0 &&
      Number.isInteger(value.sampleCount) &&
      value.sampleCount >= 0
    )
      return value;
  } catch {
    /* Storage is optional. */
  }
  return null;
}
export function clearPromptTiming(sessionId: string) {
  pending.delete(pendingKey(sessionId));
  try {
    sessionStorage.removeItem(pendingKey(sessionId));
  } catch {
    /* Storage is optional. */
  }
}
