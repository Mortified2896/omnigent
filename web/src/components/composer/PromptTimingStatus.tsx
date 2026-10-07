import { useEffect, useState } from "react";
import { Clock3Icon } from "lucide-react";
import type { PromptTiming } from "@/lib/promptTiming";

function shortDuration(seconds: number): string {
  const rounded = Math.ceil(seconds);
  return rounded < 60 ? `${Math.max(0, Math.ceil(seconds))}s` : `${Math.ceil(seconds / 60)}m`;
}
export function PromptTimingStatus({
  timing,
  now: suppliedNow,
  compact = false,
  stage,
  label = "Response timing",
}: {
  timing: PromptTiming;
  now?: number;
  compact?: boolean;
  stage?: string;
  label?: string;
}) {
  const [clock, setClock] = useState(Date.now);
  useEffect(() => {
    if (suppliedNow !== undefined) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [suppliedNow]);
  const elapsed = Math.max(0, ((suppliedNow ?? clock) - timing.startedAt) / 1000);
  const remaining = timing.estimatedSeconds - elapsed;
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
      aria-label={label}
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
          {stage ? `${stage} · ` : ""}
          {timing.sampleCount
            ? `Based on ${timing.sampleCount} completed ${timing.sampleCount === 1 ? "prompt" : "prompts"}`
            : "Learning full prompt times"}
        </p>
      </div>
    </div>
  );
}
