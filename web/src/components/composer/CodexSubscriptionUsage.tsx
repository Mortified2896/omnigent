import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";

export function CodexSubscriptionUsage({ hostId }: { hostId: string | null | undefined }) {
  return hostId ? <HostSubscriptionUsage hostId={hostId} /> : null;
}

function HostSubscriptionUsage({ hostId }: { hostId: string }) {
  const query = useQuery({
    queryKey: ["codex-subscription", getCurrentUserId(), hostId],
    enabled: !!hostId,
    staleTime: 60000,
    refetchInterval: 60000,
    retry: false,
    queryFn: async () => {
      const response = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/codex-rate-limits`,
      );
      if (!response.ok) throw new Error("Subscription usage unavailable");
      return (await response.json()) as {
        remaining_percent: number | null;
        windows: {
          name: string;
          remaining_percent: number;
          window_minutes: number | null;
          resets_at: number | null;
        }[];
      };
    },
  });
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className="block self-stretch text-center text-[10px] leading-3 text-muted-foreground tabular-nums"
          aria-label="Codex subscription remaining"
        >
          {query.data?.remaining_percent != null
            ? `${query.data.remaining_percent}% left`
            : query.isPending
              ? "Usage…"
              : "Usage unavailable"}
        </span>
      </TooltipTrigger>
      <TooltipContent className="flex-col items-start">
        {query.data?.windows?.length ? (
          query.data.windows.map((window) => (
            <span key={window.name}>
              {window.window_minutes ? `${window.window_minutes / 60}h window` : window.name}:{" "}
              {window.remaining_percent}% left
              {window.resets_at
                ? ` · Resets ${new Date(window.resets_at * 1000).toLocaleString()}`
                : ""}
            </span>
          ))
        ) : (
          <span>
            Codex subscription usage is supplied by the selected host's signed-in account.
          </span>
        )}
      </TooltipContent>
    </Tooltip>
  );
}
