import { useEffect, useState } from "react";
import { reviewDuration, type O3ReviewTiming } from "@/lib/o3ReviewTiming";

export function O3ReviewTimingStatus({ timing }: { timing: O3ReviewTiming }) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (timing.actualMs !== undefined) return;
    const timer = window.setInterval(() => setNow(performance.now()), 1000);
    return () => window.clearInterval(timer);
  }, [timing]);

  const elapsed = Math.max(0, now - timing.startedAt);
  const difference = (timing.actualMs ?? 0) - timing.estimatedMs;
  return (
    <div
      className="rounded-xl border border-border bg-card px-4 py-3 text-sm text-muted-foreground"
      data-testid="o3-review-timing"
    >
      {timing.actualMs === undefined ? (
        <>
          <p>Estimated review time: about {reviewDuration(timing.estimatedMs)}.</p>
          <p>
            {reviewDuration(elapsed)} elapsed ·{" "}
            {elapsed < timing.estimatedMs
              ? `About ${reviewDuration(timing.estimatedMs - elapsed)} remaining`
              : `Taking longer than estimated (${reviewDuration(elapsed - timing.estimatedMs)} over); still waiting for the review…`}
          </p>
        </>
      ) : (
        <p role="status">
          Review received in {reviewDuration(timing.actualMs)} · Estimated{" "}
          {reviewDuration(timing.estimatedMs)} ·{" "}
          {Math.abs(difference) < 500
            ? "On estimate"
            : `${reviewDuration(Math.abs(difference))} ${difference > 0 ? "slower" : "faster"} than estimated (${Math.round((Math.abs(difference) / timing.estimatedMs) * 100)}%)`}
          .
        </p>
      )}
      <p className="mt-1 text-xs">
        {timing.sampleCount > 0
          ? `Based on ${timing.sampleCount} recent successful review${timing.sampleCount === 1 ? "" : "s"} in this browser. Actual times vary by task and provider.`
          : "Rough starting estimate; no previous review timings in this browser yet."}
      </p>
    </div>
  );
}
