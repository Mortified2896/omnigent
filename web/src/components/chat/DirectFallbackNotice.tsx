import { TriangleAlertIcon } from "lucide-react";
import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSession } from "@/hooks/useSession";
import { useChatStore } from "@/store/chatStore";

/** Persisted provenance distinguishes a fallback from a deliberately selected Direct route. */
export function DirectFallbackNotice({ sessionId }: { sessionId: string | null }) {
  const { session } = useSession(sessionId);
  const queryClient = useQueryClient();
  const status = useChatStore((s) => s.sessionStatus);
  const previous = useRef({ sessionId, status });
  useEffect(() => {
    // Recovery can change labels without changing the URL. Refresh the shared
    // snapshot when a failed chat starts again, including a normal next message.
    if (
      sessionId &&
      previous.current.sessionId === sessionId &&
      previous.current.status === "failed" &&
      status !== "failed"
    ) {
      void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    }
    previous.current = { sessionId, status };
  }, [sessionId, status, queryClient]);
  const labels = session?.labels;
  if (labels?.["omnigent.transport.fallback_from"] !== "omniroute") return null;
  const direct = ["codex-direct", "glm-direct"].includes(labels["omnigent.access_lane"] ?? "");
  const reason = labels["omnigent.transport.fallback_reason"];
  const timestamp = Number(labels["omnigent.transport.fallback_at"]);
  const date = Number.isFinite(timestamp) && timestamp > 0 ? new Date(timestamp * 1000) : null;
  const validDate = date && Number.isFinite(date.getTime()) ? date : null;
  return (
    <div className="shrink-0 px-3 py-2 sm:px-6">
      <div
        role="alert"
        data-testid="direct-fallback-notice"
        className="mx-auto flex max-w-3xl gap-3 rounded-xl border-2 border-amber-500/70 bg-amber-100 px-4 py-3 text-amber-950 dark:bg-amber-950 dark:text-amber-100"
      >
        <TriangleAlertIcon className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
        <div className="min-w-0 text-sm">
          <p className="font-semibold">
            {direct ? "Direct fallback active" : "This chat used Direct fallback"}
          </p>
          <p>
            {direct
              ? "OmniRoute failed. This chat is continuing through a Direct connection."
              : "OmniRoute failed earlier and this chat switched to a Direct connection."}{" "}
            Investigate the gateway issue; a successful reply does not confirm it is fixed.
          </p>
          {(reason || validDate) && (
            <details className="mt-2">
              <summary className="cursor-pointer font-medium">Fallback details</summary>
              {reason && <p className="mt-1 break-words">Recorded reason: {reason}</p>}
              {validDate && (
                <time dateTime={validDate.toISOString()}>{validDate.toLocaleString()}</time>
              )}
            </details>
          )}
        </div>
      </div>
    </div>
  );
}
