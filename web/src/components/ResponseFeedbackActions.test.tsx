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
let scoringPolicy: {
  score_eligible: boolean;
  is_test: boolean;
  retention: string | null;
  responses: Record<string, unknown>;
};
const advisorRound = {
  object: "model_advisor.round",
  round_id: "round-test",
  state: "completed",
  version: 1,
  etag: "etag",
  failure_reason: null,
  review: {
    round_fingerprint: "fingerprint",
    human_choice_id: "choice-gpt",
    advisor_choice_id: "choice-gpt",
    rationale: "Both selections matched and provide the requested reasoning depth.",
    assigned_choice_id: "choice-gpt",
    assigned_arm: "same",
    human_probability_percent: 50,
    overridden: false,
    override_reason: null,
    comparison_group: "task",
  },
  execution: { session_id: "session", uncertain: false },
  decision_context: {
    schema_version: 3,
    qualified_pool: [
      {
        choice_id: "choice-gpt",
        provider: "openai",
        model_id: "gpt-6-sol",
        reasoning_effort: "high",
      },
    ],
    user_enabled_pool: [],
    advisor_visible_pool: [],
  },
};
beforeEach(() => {
  outcomes = [];
  fail = false;
  scoringPolicy = { score_eligible: true, is_test: false, retention: null, responses: {} };
  api.mockReset();
  api.mockImplementation(async (_url: string, options?: RequestInit) => {
    if (options?.method && fail) return new Response(null, { status: 500 });
    if (_url.endsWith("/task-experiment")) return Response.json(outcomes);
    if (_url.endsWith("/scoring-policy")) return Response.json(scoringPolicy);
    if (_url.includes("/response-attribution/"))
      return Response.json({
        requested_model: "gpt-6-sol",
        actual_model: "gpt-6-sol-2026-09-20",
        model_status: "observed",
        model_source: "response_usage",
        reasoning_effort: "high",
        access_lane: "openai-direct",
        advisor_round_id: "round-test",
      });
    if (_url.includes("/model-advisor/rounds/")) return Response.json(advisorRound);
    if (_url.includes("/scoring-eligibility/")) {
      const setting = JSON.parse(options!.body as string);
      scoringPolicy = { ...scoringPolicy, responses: { answer: setting } };
      return Response.json(setting);
    }
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
function mount(hostId: string | null = null, responseId = "answer") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ResponseFeedbackProvider sessionId="session" hostId={hostId}>
        <ResponseFeedbackActions responseId={responseId} />
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
    await waitFor(() => expect(screen.getByRole("button", { name })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name })).toHaveAttribute("aria-pressed", "true"),
    );
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

it("shows the model used, advisor agreement, rationale, and response trace key", async () => {
  outcomes = [
    {
      id: "outcome-1",
      kind: "outcome",
      response_id: "resp_answer_123",
      outcome: "success",
      model_attribution: {
        requested_model: "gpt-6-sol",
        actual_model: "gpt-6-sol-2026-09-20",
        model_status: "observed",
        model_source: "response_usage",
        reasoning_effort: "high",
        access_lane: "openai-direct",
        advisor_round_id: "round-test",
      },
    },
  ];
  mount("host-test", "resp_answer_123");

  const details = await screen.findByTestId("model-attribution");
  expect(details.textContent).toContain("Reported model used: gpt-6-sol-2026-09-20");
  await waitFor(() => expect(details.textContent).toContain("Your choice and the advisor agreed"));
  expect(details.textContent).toContain("Your choice: gpt-6-sol · openai · high");
  expect(details.textContent).toContain(
    "Reasoning: Both selections matched and provide the requested reasoning depth.",
  );
  expect(details.textContent).toContain("resp_answer_123");
});

it("shows response model and advisor details when an unrated answer is excluded", async () => {
  mount("host-test");
  fireEvent.click(await screen.findByRole("button", { name: "Do not score" }));

  const details = await screen.findByTestId("model-attribution");
  await waitFor(() =>
    expect(details.textContent).toContain("Reported model used: gpt-6-sol-2026-09-20"),
  );
  await waitFor(() => expect(details.textContent).toContain("Your choice and the advisor agreed"));
  expect(details.textContent).toContain("Your choice: gpt-6-sol · openai · high");
  expect(details.textContent).toContain(
    "Reasoning: Both selections matched and provide the requested reasoning depth.",
  );
  expect(screen.queryByTestId("human-review-details")).toBeNull();
});

it("keeps the saved outcome when a revision fails", async () => {
  outcomes = [{ id: "1", kind: "outcome", response_id: "answer", outcome: "not_sure" }];
  mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "Success" })).toBeEnabled());
  fail = true;
  fireEvent.click(screen.getByRole("button", { name: "Success" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Not sure" })).toHaveAttribute("aria-pressed", "true");
  expect(outcomes).toHaveLength(1);
});

it("keeps unsaved comment and tags when changing the outcome", async () => {
  outcomes = [
    {
      id: "initial",
      kind: "outcome",
      response_id: "answer",
      outcome: "partial",
      comment: "Old",
      tags: [],
    },
  ];
  mount();
  const comment = await screen.findByRole("textbox", { name: "Task review comment" });
  fireEvent.change(comment, { target: { value: "Unsaved detail" } });
  fireEvent.click(screen.getByRole("button", { name: "Tests/verification" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Custom task review tag" }), {
    target: { value: "Custom" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add tag" }));
  fireEvent.click(screen.getByRole("button", { name: "Success" }));
  await waitFor(() =>
    expect(outcomes.at(-1)).toMatchObject({
      outcome: "success",
      comment: "Unsaved detail",
      tags: ["Tests/verification", "Custom"],
    }),
  );
});
