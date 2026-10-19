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
  const labels = session?.labels;
  const fallbackStatus = labels?.["omnigent.transport.fallback_status"];
  const awaitingRecovery =
    fallbackStatus === "recovering" ||
    (status === "failed" &&
      labels?.["omnigent.access_lane"] === "omniroute" &&
      fallbackStatus !== "failed");
  useEffect(() => {
    if (!sessionId || !awaitingRecovery) return;
    // A terminal failure can switch transport while its failed status stays
    // visible. Briefly refresh that transition; stop on readiness/failure or
    // after two minutes, rather than polling every idle chat indefinitely.
    const refresh = () => void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    refresh();
    const interval = window.setInterval(refresh, 1500);
    const deadline = window.setTimeout(() => window.clearInterval(interval), 120_000);
    return () => {
      window.clearInterval(interval);
      window.clearTimeout(deadline);
    };
  }, [sessionId, awaitingRecovery, queryClient]);
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
  if (labels?.["omnigent.transport.fallback_from"] !== "omniroute") return null;
  const failed = fallbackStatus === "failed";
  const recovering = fallbackStatus === "recovering";
  const direct = ["codex-direct", "glm-direct"].includes(labels["omnigent.access_lane"] ?? "");
  const reason = labels["omnigent.transport.fallback_reason"];
  const timestamp = Number(labels["omnigent.transport.fallback_at"]);
  const date = Number.isFinite(timestamp) && timestamp > 0 ? new Date(timestamp * 1000) : null;
  const validDate = date && Number.isFinite(date.getTime()) ? date : null;
  return (
    <div className="shrink-0 px-3 pt-16 pb-2 sm:px-6 md:pt-14">
      <div
        role="alert"
        data-testid="direct-fallback-notice"
        className="mx-auto flex max-w-3xl gap-3 rounded-xl border-2 border-amber-500/70 bg-amber-100 px-4 py-3 text-amber-950 dark:bg-amber-950 dark:text-amber-100"
      >
        <TriangleAlertIcon className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
        <div className="min-w-0 text-sm">
          <p className="font-semibold">
            {failed
              ? "Direct fallback failed — OmniRoute is unavailable"
              : recovering
                ? "OmniRoute failed — switching to Direct"
                : direct
                  ? "Direct fallback active — OmniRoute failed"
                  : "This chat used Direct fallback"}
          </p>
          <p>
            {failed
              ? "The Direct connection could not be established. This chat still needs recovery."
              : recovering
                ? "Recovering the same conversation through Direct. Your history and completed tool results are preserved."
                : direct
                  ? "This chat now uses a Direct connection. Continue here with the same model and conversation history."
                  : "OmniRoute failed earlier and this chat switched to a Direct connection."}{" "}
            Investigate the gateway issue; a successful reply does not confirm it is fixed.
          </p>
          {failed && labels["omnigent.transport.fallback_error"] && (
            <p className="mt-2 font-medium">{labels["omnigent.transport.fallback_error"]}</p>
          )}
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
