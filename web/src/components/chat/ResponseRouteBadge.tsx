import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";

const labels: Record<string, string> = {
  omniroute: "OmniRoute",
  direct: "Direct",
  direct_fallback: "Direct fallback",
};

export function ResponseRouteBadge({
  sessionId,
  responseId,
  running,
}: {
  sessionId: string;
  responseId: string;
  running: boolean;
}) {
  const route = useQuery({
    queryKey: ["response-route", getCurrentUserId(), sessionId, responseId],
    queryFn: async () => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/response-route/${encodeURIComponent(responseId)}`,
      );
      if (!response.ok) throw new Error("Could not load response route");
      return (await response.json()) as { route: string | null };
    },
    refetchInterval: (query) =>
      query.state.error
        ? false
        : running || (!query.state.data?.route && query.state.dataUpdateCount < 5)
          ? 1000
          : false,
    retry: false,
  });
  const label = route.data?.route ? labels[route.data.route] : null;
  if (!label) return null;
  return (
    <p className="mb-2 text-xs font-medium text-muted-foreground" data-testid="response-route">
      {label}
    </p>
  );
}
