import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";

export type ExclusionReason = "test_fixture" | "duplicate" | "out_of_scope" | "other";
export interface ScoringEligibility {
  score_eligible: boolean;
  exclusion_reason: ExclusionReason | null;
}
export interface SessionScoringPolicy {
  score_eligible: boolean;
  is_test: boolean;
  retention: string | null;
  responses: Record<string, ScoringEligibility>;
}
const policyKey = (sessionId: string) => ["scoring-policy", getCurrentUserId(), sessionId];

export function useSessionScoringPolicy(sessionId: string) {
  return useQuery({
    queryKey: policyKey(sessionId),
    queryFn: async () => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/scoring-policy`,
      );
      if (!response.ok) throw new Error("Could not load scoring settings");
      return (await response.json()) as SessionScoringPolicy;
    },
    enabled: !!sessionId,
  });
}

export function useSaveScoringEligibility(sessionId: string, responseId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: ScoringEligibility) => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/scoring-eligibility/${encodeURIComponent(responseId)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
        },
      );
      if (!response.ok) throw new Error("Scoring setting was not saved");
      return (await response.json()) as ScoringEligibility;
    },
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: policyKey(sessionId) }),
        client.invalidateQueries({ queryKey: ["scored-outcomes", getCurrentUserId(), sessionId] }),
      ]);
    },
  });
}
