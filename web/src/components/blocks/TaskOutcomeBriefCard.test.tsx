import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TaskOutcomeBriefCard } from "./TaskOutcomeBriefCard";
import type { TaskRunDetailResponse } from "@/lib/taskOutcomes";

const mocks = vi.hoisted(() => ({
  getTaskRunForResponse: vi.fn(),
  submitTaskRunReview: vi.fn(),
}));
vi.mock("@/lib/taskOutcomes", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/taskOutcomes")>();
  return { ...actual, ...mocks };
});

const detail: TaskRunDetailResponse = {
  run: {
    id: "run-1",
    conversation_id: "conv-1",
    response_id: "resp-1",
    triggering_message_id: null,
    project_path: null,
    task_description: "task",
    proposed_task_family: "small_bug_fix",
    estimated_difficulty: null,
    harness_id: "h",
    requested_route_id: "route",
    selected_provider: "provider",
    selected_model: "model",
    reasoning_effort: "low",
    permission_mode: null,
    omniroute_decision_id: null,
    selection_strategy: null,
    billing_class: null,
    fallback_used: false,
    terminal_status: "completed",
    started_at: null,
    terminal_at: null,
    duration_ms: null,
    input_tokens: null,
    output_tokens: null,
    total_cost_usd: null,
    response_summary: null,
    changed_files: [],
    commit_sha: null,
    failure_error_code: null,
    failure_error_message: null,
    langfuse_trace_id: null,
    langfuse_observation_id: null,
    created_at: 0,
    updated_at: 0,
  },
  evaluation: {
    id: "eval-1",
    task_run_id: "run-1",
    evaluator_type: "llm",
    evaluator_provider: null,
    evaluator_model: null,
    evaluator_route_id: null,
    verdict: "success",
    confidence: 0.9,
    quality_score: 5,
    proposed_task_family: "small_bug_fix",
    reasoning: null,
    evidence: [],
    unresolved_issues: [],
    created_at: 0,
  },
  review: null,
  any_review: null,
  langfuse_pending: false,
};

describe("TaskOutcomeBriefCard actions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getTaskRunForResponse.mockResolvedValue(detail);
    mocks.submitTaskRunReview.mockResolvedValue({});
  });

  it("submits actions for the canonical persisted review", async () => {
    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await screen.findByTestId("task-outcome-brief-card");
    fireEvent.click(screen.getByRole("button", { name: /Accept/ }));
    await waitFor(() =>
      expect(mocks.submitTaskRunReview).toHaveBeenCalledWith(
        "run-1",
        expect.objectContaining({ action: "accept" }),
      ),
    );
  });
});
