import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

const review: NonNullable<TaskRunDetailResponse["review"]> = {
  id: "review-1",
  task_run_id: "run-1",
  verdict: "skipped",
  quality_score: null,
  final_task_family: null,
  evaluator_accuracy: null,
  comments: null,
  created_by: "user",
  review_action: "declined",
  learning_eligible: false,
  route_fit: null,
  failure_attribution: null,
  preferred_route_id: null,
  preferred_reasoning_effort: null,
  source_evaluation_id: null,
  review_schema_version: 1,
  created_at: 0,
  updated_at: 0,
};

function acceptedReview(): NonNullable<TaskRunDetailResponse["review"]> {
  return {
    ...review,
    id: "review-2",
    verdict: "success",
    review_action: "accepted",
    learning_eligible: true,
  };
}

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
    mocks.submitTaskRunReview.mockImplementation(
      async (_runId: string, body: { action?: string }) => {
        if (body?.action === "decline") return review;
        if (body?.action === "accept") return acceptedReview();
        return { ...review, review_action: body?.action ?? "accepted" };
      },
    );
    // Clear any leftover sessionStorage markers from prior tests.
    if (typeof window !== "undefined") {
      try {
        window.sessionStorage.clear();
      } catch {
        // sessionStorage may be unavailable; ignore.
      }
    }
  });

  afterEach(() => {
    if (typeof window !== "undefined") {
      try {
        window.sessionStorage.clear();
      } catch {
        // ignore
      }
    }
  });

  it("submits action=accept with the referenced evaluation id", async () => {
    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await screen.findByTestId("task-outcome-brief-card");
    fireEvent.click(screen.getByRole("button", { name: /Accept/ }));
    await waitFor(() =>
      expect(mocks.submitTaskRunReview).toHaveBeenCalledWith("run-1", {
        action: "accept",
        source_evaluation_id: "eval-1",
        verdict: undefined,
      }),
    );
  });

  it("opens the review form when Adjust is clicked and does not POST until save", async () => {
    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await screen.findByTestId("task-outcome-brief-card");
    fireEvent.click(screen.getByRole("button", { name: /Adjust/ }));
    // The TaskReviewCard uses these labels / attributes.
    await screen.findByTestId("task-review-card");
    expect(mocks.submitTaskRunReview).not.toHaveBeenCalled();
  });

  it("submits action=decline and the card collapses to the excluded status", async () => {
    const declinedDetail: TaskRunDetailResponse = {
      ...detail,
      review,
    };
    // First fetch: no review yet (so the brief card renders). After
    // Decline is submitted, the component re-loads and gets back a row
    // with `review_action = declined` — the card then collapses to the
    // "Excluded from routing learning" status.
    mocks.getTaskRunForResponse
      .mockResolvedValueOnce({ ...detail, review: null })
      .mockResolvedValue(declinedDetail);
    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await screen.findByTestId("task-outcome-brief-card");
    fireEvent.click(screen.getByRole("button", { name: /Decline/ }));
    await waitFor(() =>
      expect(mocks.submitTaskRunReview).toHaveBeenCalledWith("run-1", {
        action: "decline",
        source_evaluation_id: undefined,
        verdict: "skipped",
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("outcome-brief-status")).toHaveTextContent(
        /Excluded from routing learning/,
      ),
    );
  });

  it("Review later does not call submit and shows the postponed compact status", async () => {
    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await screen.findByTestId("task-outcome-brief-card");
    fireEvent.click(screen.getByRole("button", { name: /Review later/ }));
    expect(mocks.submitTaskRunReview).not.toHaveBeenCalled();
    await screen.findByTestId("outcome-brief-postponed");
    expect(screen.queryByText(/Excluded from routing learning/)).not.toBeInTheDocument();
    // The full card should be gone — no Accept / Adjust / Decline buttons shown.
    expect(screen.queryByTestId("task-outcome-brief-card")).not.toBeInTheDocument();
  });

  it("Review now restores the full card without calling submit", async () => {
    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await screen.findByTestId("task-outcome-brief-card");
    fireEvent.click(screen.getByRole("button", { name: /Review later/ }));
    await screen.findByTestId("outcome-brief-postponed");
    fireEvent.click(screen.getByRole("button", { name: /Review now/ }));
    await screen.findByTestId("task-outcome-brief-card");
    expect(mocks.submitTaskRunReview).not.toHaveBeenCalled();
  });

  it("Review later and Decline are not equivalent", async () => {
    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await screen.findByTestId("task-outcome-brief-card");

    // Click Review later — must not submit.
    fireEvent.click(screen.getByRole("button", { name: /Review later/ }));
    await screen.findByTestId("outcome-brief-postponed");
    expect(mocks.submitTaskRunReview).not.toHaveBeenCalled();

    // Restore so we can click Decline in the same rendered card.
    fireEvent.click(screen.getByRole("button", { name: /Review now/ }));
    await screen.findByTestId("task-outcome-brief-card");

    fireEvent.click(screen.getByRole("button", { name: /Decline/ }));
    await waitFor(() =>
      expect(mocks.submitTaskRunReview).toHaveBeenCalledWith(
        "run-1",
        expect.objectContaining({ action: "decline", verdict: "skipped" }),
      ),
    );
  });

  it("Review later persists across remount via sessionStorage", async () => {
    mocks.submitTaskRunReview.mockClear();
    const first = render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await first.findByTestId("task-outcome-brief-card");
    fireEvent.click(first.getByRole("button", { name: /Review later/ }));
    await first.findByTestId("outcome-brief-postponed");

    // Second mount should hydrate the postponed marker from sessionStorage
    // — no submit, and no POST yet.
    const second = render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await second.findByTestId("outcome-brief-postponed");
    expect(mocks.submitTaskRunReview).not.toHaveBeenCalled();
    second.unmount();
    first.unmount();
  });
});
