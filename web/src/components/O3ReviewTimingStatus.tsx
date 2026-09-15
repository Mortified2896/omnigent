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
  return (
    <div
      className="flex items-center justify-between gap-3 px-3 py-2 text-xs text-muted-foreground"
      data-testid="o3-review-timing"
      role="status"
    >
      <span>{timing.actualMs === undefined ? "Reviewing route" : "Review received"}</span>
      <span className="tabular-nums">
        {reviewDuration(timing.actualMs ?? elapsed)}
        {timing.actualMs === undefined ? " elapsed" : ""}
      </span>
    </div>
  );
}
