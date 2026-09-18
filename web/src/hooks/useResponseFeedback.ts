import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";

export interface ResponseFeedback {
  conversation_id: string;
  response_id: string;
  rating: 1 | -1;
  comment: string | null;
  created_at: number;
  updated_at: number;
}

export function feedbackQueryKey(sessionId: string) {
  return ["response-feedback", getCurrentUserId(), sessionId];
}

function feedbackUrl(sessionId: string, responseId?: string) {
  const base = `/v1/sessions/${encodeURIComponent(sessionId)}/response-feedback`;
  return responseId ? `${base}/${encodeURIComponent(responseId)}` : base;
}

export function useResponseFeedback(sessionId: string) {
  return useQuery({
    queryKey: feedbackQueryKey(sessionId),
    queryFn: async () => {
      const res = await authenticatedFetch(feedbackUrl(sessionId));
      if (!res.ok) throw new Error("Could not load response feedback");
      return (await res.json()) as ResponseFeedback[];
    },
    enabled: !!sessionId,
  });
}

export function useSaveResponseFeedback(sessionId: string, responseId: string) {
  const client = useQueryClient();
  const key = feedbackQueryKey(sessionId);
  return useMutation({
    mutationFn: async (payload: { rating: 1 | -1; comment?: string | null } | null) => {
      await client.cancelQueries({ queryKey: key });
      const res = await authenticatedFetch(feedbackUrl(sessionId, responseId), {
        method: payload ? "PUT" : "DELETE",
        ...(payload
          ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }
          : {}),
      });
      if (!res.ok) throw new Error("Feedback was not saved. Please try again.");
      return payload ? ((await res.json()) as ResponseFeedback) : null;
    },
    onSuccess: async (saved) => {
      await client.cancelQueries({ queryKey: key });
      client.setQueryData<ResponseFeedback[]>(key, (previous = []) => {
        const remaining = previous.filter((row) => row.response_id !== responseId);
        return saved ? [...remaining, saved] : remaining;
      });
    },
  });
}
