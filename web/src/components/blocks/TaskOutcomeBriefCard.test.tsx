import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskOutcomeBriefCard } from "./TaskOutcomeBriefCard";
import { TaskRunFetchError, TaskRunResponseIdentityError } from "@/lib/taskOutcomes";
import type { TaskRunDetailResponse } from "@/lib/taskOutcomes";

const mocks = vi.hoisted(() => ({
  getTaskRunForResponse: vi.fn(),
  getTaskRun: vi.fn(),
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

type DetailOverrides = Omit<Partial<TaskRunDetailResponse>, "run" | "evaluation" | "review"> & {
  run?: Partial<TaskRunDetailResponse["run"]>;
  evaluation?: TaskRunDetailResponse["evaluation"];
  review?: TaskRunDetailResponse["review"];
};

function detailFor(overrides: DetailOverrides = {}): TaskRunDetailResponse {
  return {
    ...detail,
    ...overrides,
    run: { ...detail.run, ...overrides.run },
    evaluation: overrides.evaluation === undefined ? detail.evaluation : overrides.evaluation,
    review: overrides.review === undefined ? detail.review : overrides.review,
  };
}

async function flushAsync(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

async function advanceTimers(ms: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    await Promise.resolve();
  });
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
    mocks.getTaskRun.mockResolvedValue(detail);
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
    vi.useRealTimers();
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

  it("polls with a real refetch until a late evaluation appears, then Accept submits it", async () => {
    vi.useFakeTimers();
    mocks.getTaskRunForResponse
      .mockResolvedValueOnce(detailFor({ evaluation: null }))
      .mockResolvedValueOnce(detail);

    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await flushAsync();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("outcome-brief-pending")).toHaveTextContent(
      /Preparing outcome brief/,
    );

    await advanceTimers(800);
    expect(screen.getByTestId("task-outcome-brief-card")).toBeInTheDocument();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/Likely success · small_bug_fix · Quality 5\/5/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Accept/ }));
    await flushAsync();
    expect(mocks.submitTaskRunReview).toHaveBeenCalledWith("run-1", {
      action: "accept",
      source_evaluation_id: "eval-1",
      verdict: undefined,
    });
  });

  it("retries an initially missing task run and renders the later run with evaluation", async () => {
    vi.useFakeTimers();
    mocks.getTaskRunForResponse
      .mockRejectedValueOnce(new TaskRunFetchError(404))
      .mockResolvedValueOnce(detail);

    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await flushAsync();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("outcome-brief-pending")).toBeInTheDocument();

    await advanceTimers(800);
    expect(screen.getByTestId("task-outcome-brief-card")).toBeInTheDocument();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(2);
  });

  it("exhausts repeated 404s without infinite timers and Retry starts a new cycle", async () => {
    vi.useFakeTimers();
    mocks.getTaskRunForResponse.mockRejectedValue(new TaskRunFetchError(404));

    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await flushAsync();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);

    await advanceTimers(800);
    await advanceTimers(1_600);
    await advanceTimers(3_200);
    await advanceTimers(6_400);
    expect(screen.getByTestId("outcome-brief-exhausted")).toBeInTheDocument();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(5);

    await advanceTimers(60_000);
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(5);

    fireEvent.click(screen.getByTestId("outcome-brief-retry"));
    await flushAsync();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(6);
  });

  it("exhausts evaluation-null responses without rendering a fabricated verdict", async () => {
    vi.useFakeTimers();
    mocks.getTaskRunForResponse.mockResolvedValue(detailFor({ evaluation: null }));

    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await flushAsync();
    await advanceTimers(800);
    await advanceTimers(1_600);
    await advanceTimers(3_200);
    await advanceTimers(6_400);

    expect(screen.getByTestId("outcome-brief-evaluation-unavailable")).toBeInTheDocument();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(5);
    expect(screen.queryByText(/Likely unsure/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Accept/ })).not.toBeInTheDocument();
  });

  it("unmount cancels a scheduled poll and aborts the in-flight request", async () => {
    vi.useFakeTimers();
    mocks.getTaskRunForResponse.mockResolvedValueOnce(detailFor({ evaluation: null }));
    const rendered = render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await flushAsync();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);
    const firstSignal = mocks.getTaskRunForResponse.mock.calls[0][2] as AbortSignal;
    expect(firstSignal.aborted).toBe(false);

    rendered.unmount();
    expect(firstSignal.aborted).toBe(true);
    await advanceTimers(60_000);
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);
  });

  it("changing session or response prevents the prior request from overwriting new state", async () => {
    let resolveOld!: (value: TaskRunDetailResponse) => void;
    const oldPromise = new Promise<TaskRunDetailResponse>((resolve) => {
      resolveOld = resolve;
    });
    const newDetail = detailFor({
      run: {
        id: "run-2",
        conversation_id: "conv-2",
        response_id: "resp-2",
      },
      evaluation: { ...detail.evaluation!, id: "eval-2", task_run_id: "run-2" },
    });
    mocks.getTaskRunForResponse.mockReturnValueOnce(oldPromise).mockResolvedValueOnce(newDetail);

    const rendered = render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await flushAsync();
    const oldSignal = mocks.getTaskRunForResponse.mock.calls[0][2] as AbortSignal;

    rendered.rerender(<TaskOutcomeBriefCard sessionId="conv-2" responseId="resp-2" />);
    await flushAsync();
    await screen.findByTestId("task-outcome-brief-card");
    expect(oldSignal.aborted).toBe(true);
    expect(screen.getByTestId("task-outcome-brief-card")).toHaveAttribute(
      "data-task-run-id",
      "run-2",
    );

    await act(async () => {
      resolveOld(detail);
      await oldPromise;
    });
    expect(screen.getByTestId("task-outcome-brief-card")).toHaveAttribute(
      "data-task-run-id",
      "run-2",
    );
  });

  it("does not start overlapping poll requests while the previous request is slow", async () => {
    vi.useFakeTimers();
    let resolveSlow!: (value: TaskRunDetailResponse) => void;
    const slow = new Promise<TaskRunDetailResponse>((resolve) => {
      resolveSlow = resolve;
    });
    mocks.getTaskRunForResponse
      .mockReturnValueOnce(slow)
      .mockResolvedValueOnce(detailFor({ evaluation: null }));

    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await flushAsync();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);

    await advanceTimers(8_000);
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveSlow(detailFor({ evaluation: null }));
      await slow;
    });
    await advanceTimers(799);
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);
    await advanceTimers(1);
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(2);
  });

  it("removes the card and stops polling when the endpoint returns another response's run", async () => {
    mocks.getTaskRunForResponse.mockRejectedValue(
      new TaskRunResponseIdentityError("resp-1", "resp-other"),
    );

    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await flushAsync();

    expect(screen.queryByTestId("outcome-brief-pending")).not.toBeInTheDocument();
    expect(screen.queryByTestId("outcome-brief-failed")).not.toBeInTheDocument();
    expect(mocks.getTaskRunForResponse).toHaveBeenCalledTimes(1);
  });

  it("saving Adjust submits action=adjust with the corrected fields", async () => {
    render(<TaskOutcomeBriefCard sessionId="conv-1" responseId="resp-1" />);
    await screen.findByTestId("task-outcome-brief-card");
    fireEvent.click(screen.getByRole("button", { name: /Adjust/ }));
    const reviewCard = await screen.findByTestId("task-review-card");
    expect(mocks.submitTaskRunReview).not.toHaveBeenCalled();

    fireEvent.click(within(reviewCard).getByText("Partially successful"));
    fireEvent.click(within(reviewCard).getByRole("button", { name: "4" }));
    fireEvent.click(within(reviewCard).getByText("incorrect"));
    const selects = within(reviewCard).getAllByRole("combobox");
    fireEvent.change(selects[0], { target: { value: "too_weak" } });
    fireEvent.change(selects[3], { target: { value: "backend_api" } });
    fireEvent.change(within(reviewCard).getByPlaceholderText(/Optional notes/), {
      target: { value: "Corrected outcome after manual review." },
    });

    fireEvent.click(within(reviewCard).getByRole("button", { name: /Save review/ }));
    await waitFor(() =>
      expect(mocks.submitTaskRunReview).toHaveBeenCalledWith(
        "run-1",
        expect.objectContaining({
          action: "adjust",
          source_evaluation_id: "eval-1",
          verdict: "partial",
          quality_score: 4,
          final_task_family: "backend_api",
          route_fit: "too_weak",
          evaluator_accuracy: "incorrect",
          comments: "Corrected outcome after manual review.",
        }),
      ),
    );
  });
});
