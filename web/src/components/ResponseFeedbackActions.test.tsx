import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  ResponseFeedbackActions,
  ResponseFeedbackProvider,
  canRateResponse,
} from "./ResponseFeedbackActions";
import type { Bubble } from "@/lib/renderItems";

const api = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: api, getCurrentUserId: () => "local" }));
let fail: boolean;
let outcomes: Record<string, unknown>[];
beforeEach(() => {
  outcomes = [];
  fail = false;
  api.mockReset();
  api.mockImplementation(async (_url: string, options?: RequestInit) => {
    if (options?.method && fail) return new Response(null, { status: 500 });
    if (_url.endsWith("/task-experiment")) return Response.json(outcomes);
    if (_url.includes("/task-outcomes/")) {
      const row = {
        id: String(outcomes.length),
        kind: "outcome",
        response_id: "answer",
        review_source: "human",
        ...JSON.parse(options!.body as string),
      };
      outcomes.push(row);
      return Response.json(row);
    }
    return Response.json([]);
  });
});
afterEach(cleanup);
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ResponseFeedbackProvider sessionId="session">
        <ResponseFeedbackActions responseId="answer" />
      </ResponseFeedbackProvider>
    </QueryClientProvider>,
  );
}
it("only offers feedback for durable completed visible answers", () => {
  const bubble: Bubble = {
    kind: "assistant",
    responseId: "answer",
    stableId: "answer",
    lifecycle: "completed",
    error: null,
    items: [{ kind: "text", itemId: "persisted", text: "Answer", final: true }],
  };
  expect(canRateResponse(bubble)).toBe(true);
  for (const lifecycle of ["streaming", "failed", "cancelled", "incomplete"] as const)
    expect(canRateResponse({ ...bubble, lifecycle })).toBe(false);
  expect(canRateResponse({ ...bubble, continued: true })).toBe(false);
  expect(canRateResponse({ ...bubble, items: [] })).toBe(false);
  expect(
    canRateResponse({
      ...bubble,
      items: [{ kind: "text", itemId: null, text: "Process", final: false }],
    }),
  ).toBe(false);
  expect(canRateResponse({ kind: "user", itemId: "user", content: [] })).toBe(false);
  expect(
    canRateResponse({
      kind: "routing_decision",
      itemId: "route",
      model: "test",
      applied: true,
      rationale: "routing",
    }),
  ).toBe(false);
});

it("preserves all outcome revisions across reload", async () => {
  let view = mount();
  /* eslint-disable no-await-in-loop */
  for (const name of ["Success", "Partial", "Failed", "Not sure"]) {
    const button = screen.getByRole("button", { name });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    view.unmount();
    view = mount();
    await waitFor(() =>
      expect(screen.getByRole("button", { name })).toHaveAttribute("aria-pressed", "true"),
    );
  }
  /* eslint-enable no-await-in-loop */
  fireEvent.click(screen.getByRole("button", { name: "Success" }));
  await waitFor(() => expect(outcomes.at(-1)?.outcome).toBe("success"));
  expect(outcomes.map((row) => row.outcome)).toEqual([
    "success",
    "partial",
    "failed",
    "not_sure",
    "success",
  ]);
  expect(screen.queryByRole("button", { name: "Good response" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Bad response" })).toBeNull();
});

it("appends human comment and tag revisions", async () => {
  outcomes = [
    {
      id: "1",
      kind: "outcome",
      response_id: "answer",
      outcome: "partial",
      comment: null,
      tags: [],
      review_source: "human",
    },
  ];
  mount();
  await screen.findByTestId("human-review-details");
  fireEvent.change(screen.getByLabelText("Task review comment"), {
    target: { value: "Tests still need to pass." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Tests/verification" }));
  fireEvent.change(screen.getByLabelText("Custom task review tag"), {
    target: { value: "Regression" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add tag" }));
  fireEvent.click(screen.getByRole("button", { name: "Save details" }));
  await waitFor(() => expect(outcomes).toHaveLength(2));
  expect(outcomes.at(-1)).toMatchObject({
    outcome: "partial",
    comment: "Tests still need to pass.",
    tags: ["Tests/verification", "Regression"],
  });
});

it("shows model self-review separately from the human outcome", async () => {
  outcomes = [
    {
      id: "model-1",
      kind: "model_review",
      response_id: "answer",
      outcome: "success",
      confidence: 0.86,
      comment: "The requested implementation and verification completed.",
      tags: ["Tests/verification"],
      review_source: "model",
    },
    {
      id: "human-1",
      kind: "outcome",
      response_id: "answer",
      outcome: "not_sure",
      comment: null,
      tags: [],
      review_source: "human",
    },
  ];
  mount();
  const model = await screen.findByTestId("model-self-review");
  expect(model).toHaveTextContent("Model self-review");
  expect(model).toHaveTextContent("Success");
  expect(model).toHaveTextContent("86%");
  expect(screen.getByRole("button", { name: "Not sure" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

it("keeps the saved outcome when a revision fails", async () => {
  outcomes = [{ id: "1", kind: "outcome", response_id: "answer", outcome: "not_sure" }];
  mount();
  const success = screen.getByRole("button", { name: "Success" });
  await waitFor(() => expect(success).toBeEnabled());
  fail = true;
  fireEvent.click(success);
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Not sure" })).toHaveAttribute("aria-pressed", "true");
  expect(outcomes).toHaveLength(1);
});
