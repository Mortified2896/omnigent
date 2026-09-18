import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";

export type TaskOutcome = "success" | "partial" | "failed" | "not_sure";
export interface TaskOutcomeInput {
  outcome: TaskOutcome;
  comment?: string | null;
  tags?: string[];
}
export interface ExperimentEvent {
  id: string;
  kind: string;
  response_id: string;
  outcome?: TaskOutcome;
  first_attempt_success?: 0 | 1 | null;
  confidence?: number;
  comment?: string | null;
  tags?: string[];
  evidence?: string[];
  review_source?: "human" | "model";
  provenance?: Record<string, unknown>;
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
    mutationFn: async (input: TaskOutcomeInput) => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/task-outcomes/${encodeURIComponent(responseId)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
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
