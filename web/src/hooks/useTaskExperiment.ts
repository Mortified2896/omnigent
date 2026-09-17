import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";

export type TaskOutcome = "success" | "partial" | "failed" | "not_sure";
export interface ExperimentEvent {
  id: string;
  attempt_id: string;
  human_probability?: number | null;
  selected_model?: string | null;
  selected_reasoning_effort?: string | null;
  experiment_source?: string;
  probability?: number | null;
  forecaster_id?: string;
  status?: string;
  rationale?: string;
  alternative?: { canonical_model: string; compute_profile: string; probability: number } | null;
  kind: string;
  response_id: string;
  outcome?: TaskOutcome;
  first_attempt_success?: 0 | 1 | null;
  created_at: number;
}
const key = (sessionId: string) => ["task-experiment", getCurrentUserId(), sessionId];
export function useTaskExperiment(sessionId: string) {
  return useQuery({
    queryKey: key(sessionId),
    queryFn: async () => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/task-experiment`,
      );
      if (!response.ok) throw new Error("Could not load task outcomes");
      return (await response.json()) as ExperimentEvent[];
    },
    enabled: !!sessionId,
  });
}
export function useSaveTaskOutcome(sessionId: string, responseId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (outcome: TaskOutcome) => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/task-outcomes/${encodeURIComponent(responseId)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ outcome }),
        },
      );
      if (!response.ok) throw new Error("Task outcome was not saved");
      return (await response.json()) as ExperimentEvent;
    },
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: key(sessionId) });
    },
  });
}
