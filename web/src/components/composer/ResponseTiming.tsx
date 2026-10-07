import { useEffect, useState } from "react";
import { PromptTimingStatus } from "./PromptTimingStatus";
import {
  readPromptTiming,
  clearPromptTiming,
  recordPromptDuration,
  startPromptTiming,
  type PromptTiming,
} from "@/lib/promptTiming";
import { getCurrentUserId } from "@/lib/identity";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { buildBubbles, type Bubble } from "@/lib/renderItems";

interface Run {
  start: number;
  profile: string;
  responseId: string | null;
  timing: PromptTiming;
}
const runs = new Map<string, Run>();
function storageKey(profile: string) {
  return `omnigent.response-times:${getCurrentUserId()}:${profile}`;
}
function observations(profile: string): number[] {
  try {
    const data: unknown = JSON.parse(localStorage.getItem(storageKey(profile)) ?? "[]");
    return Array.isArray(data)
      ? data
          .filter(
            (n): n is number => typeof n === "number" && Number.isFinite(n) && n > 0 && n < 86400,
          )
          .slice(-20)
      : [];
  } catch {
    return [];
  }
}
export function ResponseTiming({
  sessionId,
  compact = false,
  fallbackRunning = false,
}: {
  sessionId: string | null;
  compact?: boolean;
  fallbackRunning?: boolean;
}) {
  const state = useConversationEntryState(sessionId);
  const running =
    state.status === "streaming" ||
    state.sessionStatus === "running" ||
    state.sessionStatus === "waiting";
  const handedOff = sessionId ? readPromptTiming(sessionId) : null;
  const starting = Boolean(
    handedOff && !running && state.sessionStatus !== "failed" && !runs.has(sessionId!),
  );
  const [now, setNow] = useState(Date.now);
  const profile = JSON.stringify([
    state.sessionHarness,
    state.sessionModelOverride ?? state.llmModel,
    state.sessionReasoningEffort,
  ]);
  useEffect(() => {
    if (!sessionId) return;
    if (state.sessionStatus === "failed") clearPromptTiming(sessionId);
    if (running && !runs.has(sessionId)) {
      const timing =
        readPromptTiming(sessionId) ??
        startPromptTiming(state.sendLatchedAt ?? Date.now(), observations(profile));
      runs.set(sessionId, {
        start: timing.startedAt,
        timing,
        profile,
        responseId:
          buildBubbles(state.blocks, state.activeResponse)
            .filter(
              (b): b is Extract<Bubble, { kind: "assistant" }> =>
                b.kind === "assistant" && b.lifecycle === "completed",
            )
            .at(-1)?.responseId ?? null,
      });
    }
    if (!running) {
      const run = runs.get(sessionId);
      if (run) {
        const completed = buildBubbles(state.blocks, state.activeResponse)
          .filter((b) => b.kind === "assistant" && b.lifecycle === "completed")
          .at(-1);
        // Learn only from successful, observed replies; interruptions/errors do
        // not become fast-success samples. Both displays share this one record.
        if (
          completed?.kind === "assistant" &&
          completed.responseId !== run.responseId &&
          completed.workedForS != null
        ) {
          const duration = Math.max(1, (Date.now() - run.start) / 1000);
          const samples = [...observations(run.profile), duration].slice(-20);
          recordPromptDuration(duration);
          try {
            localStorage.setItem(storageKey(run.profile), JSON.stringify(samples));
          } catch {
            /* Estimates still work with a heuristic. */
          }
        }
        runs.delete(sessionId);
        clearPromptTiming(sessionId);
      }
      return;
    }
  }, [
    sessionId,
    running,
    profile,
    state.sendLatchedAt,
    state.blocks,
    state.activeResponse,
    state.sessionStatus,
  ]);
  useEffect(() => {
    if (!running && !starting) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, starting]);
  if (!running && !starting)
    return compact && fallbackRunning ? (
      <span className="whitespace-nowrap text-brand-accent">Running</span>
    ) : null;
  const run = sessionId ? runs.get(sessionId) : null;
  const start = run?.start ?? state.sendLatchedAt ?? now;
  const timing =
    run?.timing ??
    (sessionId ? readPromptTiming(sessionId) : null) ??
    startPromptTiming(start, observations(profile));
  return <PromptTimingStatus timing={timing} now={now} compact={compact} />;
}
