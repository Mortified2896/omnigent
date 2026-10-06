import { useEffect, useState } from "react";
import { Clock3Icon } from "lucide-react";
import { getCurrentUserId } from "@/lib/identity";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { buildBubbles, type Bubble } from "@/lib/renderItems";

interface Run {
  start: number;
  profile: string;
  responseId: string | null;
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
export function estimateDuration(samples: readonly number[]): number {
  if (!samples.length) return 60;
  const sorted = [...samples].sort((a, b) => a - b);
  return Math.max(1, sorted[Math.floor((sorted.length - 1) * 0.75)]!);
}
export function shortDuration(seconds: number): string {
  return seconds < 60 ? `${Math.max(0, Math.ceil(seconds))}s` : `${Math.ceil(seconds / 60)}m`;
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
  const [now, setNow] = useState(Date.now);
  const profile = JSON.stringify([
    state.sessionHarness,
    state.sessionModelOverride ?? state.llmModel,
    state.sessionReasoningEffort,
  ]);
  useEffect(() => {
    if (!sessionId) return;
    if (running && !runs.has(sessionId))
      runs.set(sessionId, {
        start: state.sendLatchedAt ?? Date.now(),
        profile,
        responseId:
          buildBubbles(state.blocks, state.activeResponse)
            .filter(
              (b): b is Extract<Bubble, { kind: "assistant" }> =>
                b.kind === "assistant" && b.lifecycle === "completed",
            )
            .at(-1)?.responseId ?? null,
      });
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
          const samples = [...observations(run.profile), completed.workedForS].slice(-20);
          try {
            localStorage.setItem(storageKey(run.profile), JSON.stringify(samples));
          } catch {
            /* Estimates still work with a heuristic. */
          }
        }
        runs.delete(sessionId);
      }
      return;
    }
  }, [sessionId, running, profile, state.sendLatchedAt, state.blocks, state.activeResponse]);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  if (!running)
    return compact && fallbackRunning ? (
      <span className="whitespace-nowrap text-brand-accent">Running</span>
    ) : null;
  const run = sessionId ? runs.get(sessionId) : null;
  const start = run?.start ?? state.sendLatchedAt ?? now;
  const elapsed = Math.max(0, (now - start) / 1000);
  const samples = observations(run?.profile ?? profile);
  const remaining = estimateDuration(samples) - elapsed;
  const estimate = remaining > 0 ? `~${shortDuration(remaining)} left` : "Taking longer";
  if (compact)
    return (
      <span
        className="whitespace-nowrap font-medium tabular-nums text-brand-accent"
        title={`${estimate} · ${shortDuration(elapsed)} elapsed`}
      >
        {estimate}
      </span>
    );
  return (
    <div
      role="status"
      aria-label="Response timing"
      className="mb-3 flex items-center gap-3 rounded-lg border border-brand-accent/25 bg-brand-accent/5 px-3 py-3 text-brand-accent"
    >
      <Clock3Icon className="size-5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="text-base font-semibold tabular-nums">
          {remaining > 0
            ? `About ${shortDuration(remaining)} left`
            : "Taking longer than estimated"}
        </p>
        <p className="text-xs text-muted-foreground">
          {shortDuration(elapsed)} elapsed
          {samples.length
            ? ` · Based on ${samples.length} completed ${samples.length === 1 ? "reply" : "replies"}`
            : " · Learning response times"}
        </p>
      </div>
    </div>
  );
}
