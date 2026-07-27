import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import type { Bubble } from "./renderItems";
import {
  TASK_RUN_DISCOVERY_DELAYS_MS,
  useTaskOutcomeRuns,
  type TaskRunSummary,
} from "./taskOutcomes";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);

function response(runs: TaskRunSummary[]) {
  return { ok: true, json: async () => ({ object: "list", runs }) } as Response;
}

const bubble: Bubble = {
  kind: "assistant",
  responseId: "native-response-gamma",
  stableId: "assistant-stable-beta",
  lifecycle: "completed",
  error: null,
  items: [{ kind: "text", itemId: "answer", text: "Hi!", final: true }],
};
const user: Bubble = {
  kind: "user",
  itemId: "user-alpha",
  content: [{ type: "input_text", text: "Hi" }],
};
const delayedRun: TaskRunSummary = {
  id: "run-1",
  conversation_id: "session-a",
  response_id: "execution-delta",
  triggering_message_id: "user-alpha",
  terminal_status: "completed",
  started_at: 1,
  terminal_at: 2,
  duration_ms: 1,
  selected_provider: null,
  selected_model: null,
  requested_route_id: null,
  fallback_used: null,
  harness_id: "opencode-native",
  proposed_task_family: null,
  input_tokens: null,
  output_tokens: null,
  total_cost_usd: null,
  commit_sha: null,
  changed_files_count: null,
  failure_error_code: null,
  langfuse_trace_id: null,
  created_at: 1,
  updated_at: 2,
};

describe("useTaskOutcomeRuns discovery", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
  });

  it("discovers a run persisted after idle without speculative registry entries", async () => {
    fetchMock
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response([delayedRun]));
    const { result, rerender } = renderHook(
      ({ status }: { status: "idle" | "streaming" }) =>
        useTaskOutcomeRuns("session-a", status, [user, bubble]),
      { initialProps: { status: "streaming" } },
    );

    await act(async () => Promise.resolve());
    expect(result.current).toHaveLength(0);
    rerender({ status: "idle" });
    await act(async () => Promise.resolve());
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(TASK_RUN_DISCOVERY_DELAYS_MS[0]);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.current.map((run) => run.id)).toEqual(["run-1"]);
  });

  it("stops after the bounded schedule", async () => {
    fetchMock.mockResolvedValue(response([]));
    const { rerender } = renderHook(
      ({ status }: { status: "idle" | "streaming" }) => useTaskOutcomeRuns("s", status),
      { initialProps: { status: "streaming" } },
    );
    await act(async () => Promise.resolve());
    rerender({ status: "idle" });
    await act(async () => Promise.resolve());
    for (const delay of TASK_RUN_DISCOVERY_DELAYS_MS) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(delay);
      });
    }
    expect(fetchMock).toHaveBeenCalledTimes(2 + TASK_RUN_DISCOVERY_DELAYS_MS.length);
  });
});
