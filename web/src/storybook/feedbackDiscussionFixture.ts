import type { QueryClient } from "@tanstack/react-query";
import type { ExperimentEvent, TaskOutcomeInput } from "@/hooks/useTaskExperiment";
import type { ScoringEligibility, SessionScoringPolicy } from "@/hooks/useScoringEligibility";
import { getCurrentUserId } from "@/lib/identity";

export const ORIGINAL_RESPONSE_ID = "storybook-original-answer";
export const SEED_FEEDBACK = {
  outcome: "partial" as const,
  comment: "Needs live verification.",
  tags: ["Tests/verification"],
};
const ATTRIBUTION: NonNullable<ExperimentEvent["model_attribution"]> = {
  requested_model: "gpt-5.4",
  actual_model: "gpt-5.4",
  model_status: "observed",
  model_source: "response_usage",
  reasoning_effort: "high",
  access_lane: null,
  advisor_round_id: null,
};

/** Implements the real feedback API contract in memory. Never forwards a request. */
export function createFeedbackDiscussionFixture(
  sessionId: string,
  withSavedFeedback: boolean,
  seedFeedback: TaskOutcomeInput = SEED_FEEDBACK,
) {
  const rows = new Map<string, ExperimentEvent>();
  const requests: { path: string; method: string }[] = [];
  const policy: SessionScoringPolicy = {
    score_eligible: true,
    // Simulate an ordinary production chat's UI state; no real session or scorer exists.
    is_test: false,
    retention: null,
    responses: {},
  };
  let sequence = 0;
  function save(responseId: string, input: TaskOutcomeInput): ExperimentEvent {
    const row: ExperimentEvent = {
      id: `storybook-outcome-${++sequence}`,
      kind: "outcome",
      response_id: responseId,
      outcome: input.outcome,
      comment: input.comment ?? null,
      tags: [...(input.tags ?? [])],
      review_source: "human",
      model_attribution: ATTRIBUTION,
      created_at: Date.now() / 1000,
    };
    rows.set(responseId, row);
    return row;
  }
  if (withSavedFeedback) save(ORIGINAL_RESPONSE_ID, seedFeedback);
  const prefix = `/v1/sessions/${encodeURIComponent(sessionId)}/`;
  const json = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  const fetcher = async (path: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    requests.push({ path, method });
    const route = path.startsWith(prefix) ? path.slice(prefix.length) : null;
    if (method === "GET" && route === "task-experiment") return json([...rows.values()]);
    if (method === "GET" && route === "scoring-policy") return json(policy);
    if (method === "GET" && route?.startsWith("response-attribution/")) return json(ATTRIBUTION);
    if (method === "PUT" && route?.startsWith("task-outcomes/")) {
      const input = JSON.parse(String(init?.body)) as TaskOutcomeInput;
      return json(save(decodeURIComponent(route.slice("task-outcomes/".length)), input));
    }
    if (method === "PUT" && route?.startsWith("scoring-eligibility/")) {
      const input = JSON.parse(String(init?.body)) as ScoringEligibility;
      policy.responses[decodeURIComponent(route.slice("scoring-eligibility/".length))] = input;
      return json(input);
    }
    // Unimplemented calls stay inside the fixture as well, including any accidental real IDs.
    return json({ error: "This endpoint is unavailable in the Storybook fixture." }, 404);
  };
  return {
    fetcher,
    requests,
    seed(client: QueryClient) {
      client.setQueryData(["task-experiment", getCurrentUserId(), sessionId], [...rows.values()]);
      client.setQueryData(["scoring-policy", getCurrentUserId(), sessionId], policy);
    },
    savedFeedback() {
      const row = rows.get(ORIGINAL_RESPONSE_ID);
      return row?.outcome
        ? { outcome: row.outcome, comment: row.comment ?? "", tags: [...(row.tags ?? [])] }
        : null;
    },
  };
}
