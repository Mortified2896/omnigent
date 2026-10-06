import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  ResponseFeedbackActions,
  ResponseFeedbackProvider,
  canRateResponse,
  type ReviewPerspectiveInput,
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
let advisorRoundAttached: boolean;
const advisorRound = {
  object: "model_advisor.round",
  round_id: "round-test",
  state: "completed",
  version: 1,
  etag: "etag",
  failure_reason: null,
  review: {
    round_fingerprint: "fingerprint",
    human_choice_id: "choice-human",
    advisor_choice_id: "choice-gpt",
    rationale: "This task needs the stronger reasoning level for a reliable implementation.",
    assigned_choice_id: "choice-gpt",
    assigned_arm: "advisor",
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
        choice_id: "choice-human",
        provider: "glm",
        model_id: "glm-5.3",
        reasoning_effort: "low",
      },
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
  advisorRoundAttached = true;
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
        advisor_round_id: advisorRoundAttached ? "round-test" : null,
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
function mount(
  hostId: string | null = null,
  responseId = "answer",
  compact = false,
  autoSave = false,
  renderPerspective?: (review: ReviewPerspectiveInput) => ReactNode,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ResponseFeedbackProvider sessionId="session" hostId={hostId}>
        <ResponseFeedbackActions
          responseId={responseId}
          compact={compact}
          autoSave={autoSave}
          renderPerspective={renderPerspective}
        />
      </ResponseFeedbackProvider>
    </QueryClientProvider>,
  );
}
it("lets the compact scoring switch exclude and restore an answer without changing its review", async () => {
  outcomes = [
    {
      id: "review",
      kind: "outcome",
      response_id: "answer",
      outcome: "success",
      comment: "Keep this note",
      tags: [],
    },
  ];
  mount(null, "answer", true);
  await screen.findByLabelText("Task review comment");
  const toggle = await screen.findByRole("switch", { name: "Do not score" });
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
  expect(await screen.findByLabelText("Scoring exclusion reason")).toBeVisible();
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));
  expect(screen.queryByLabelText("Scoring exclusion reason")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Task review comment")).toHaveValue("Keep this note");
  expect(outcomes).toHaveLength(1);
});
it("shows compact choices and marks the assigned option without repeating it in another row", async () => {
  mount("host-test", "answer", true);
  await waitFor(() => expect(screen.getByRole("button", { name: "Partial" })).toBeEnabled());
  expect(screen.queryByTestId("model-attribution")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("switch", { name: "Do not score" }));
  await waitFor(() =>
    expect(screen.getByRole("switch", { name: "Do not score" })).toHaveAttribute(
      "aria-checked",
      "true",
    ),
  );
  expect(screen.queryByTestId("model-attribution")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Partial" }));
  const comment = await screen.findByLabelText("Task review comment");
  expect(screen.getByText("Show model decision").closest("details")).not.toHaveAttribute("open");
  fireEvent.change(comment, { target: { value: "Unbiased first impression" } });
  fireEvent.click(screen.getByText("Show model decision"));
  const selected = await screen.findByLabelText("Advisor’s choice · Selected");
  expect(selected).toHaveAttribute("data-selected", "true");
  expect(selected).toHaveTextContent("GPT-6 Sol");
  expect(selected).toHaveTextContent("high reasoning");
  expect(screen.getByLabelText("Your choice")).toHaveAttribute("data-selected", "false");
  expect(screen.getByLabelText("Your choice")).toHaveTextContent("glm-5.3");
  expect(screen.getByText(advisorRound.review.rationale)).toBeVisible();
  expect(screen.queryByText("Execution details")).not.toBeInTheDocument();
  expect(screen.queryByText(/Response \/ trace key/)).not.toBeInTheDocument();
  expect(screen.getByLabelText("Task review comment")).toHaveValue("Unbiased first impression");
});
it("keeps the no-Advisor state clear and marks only a reported matching choice", async () => {
  advisorRoundAttached = false;
  mount("host-test", "answer", true);
  await waitFor(() => expect(screen.getByRole("button", { name: "Partial" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Partial" }));
  fireEvent.click(await screen.findByText("Show model decision"));
  await waitFor(() => expect(screen.getByLabelText("Your choice")).toHaveTextContent("GPT-6 Sol"));
  expect(screen.getByLabelText("Advisor’s choice")).toHaveTextContent("Not used");
  // A versioned reported model differs from the requested ID: do not claim a match.
  expect(screen.queryByLabelText("Selected")).not.toBeInTheDocument();
});
it("hydrates saved reviews, autosaves tags and debounced comments without remounting the editor", async () => {
  outcomes = [
    {
      id: "existing",
      kind: "outcome",
      response_id: "answer",
      outcome: "partial",
      comment: "Initial note",
      tags: [],
    },
  ];
  mount(null, "answer", true, true);
  const comment = await screen.findByLabelText("Task review comment");
  await waitFor(() => expect(comment).toHaveValue("Initial note"));
  expect(screen.queryByRole("button", { name: "Save details" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Tests/verification" }));
  await waitFor(() => expect(outcomes.at(-1)?.tags).toEqual(["Tests/verification"]));
  fireEvent.change(comment, { target: { value: "First draft" } });
  fireEvent.change(comment, { target: { value: "  Final note  " } });
  await waitFor(() => expect(outcomes.at(-1)?.comment).toBe("Final note"));
  expect(screen.getByLabelText("Task review comment")).toBe(comment);
  expect(comment).toHaveValue("  Final note  ");
  await waitFor(() =>
    expect(screen.getByLabelText("Feedback save status")).toHaveTextContent("Saved"),
  );
  expect(outcomes).toHaveLength(3);
});
it("shows a failed autosave without losing the draft and supports an explicit retry", async () => {
  outcomes = [
    {
      id: "existing",
      kind: "outcome",
      response_id: "answer",
      outcome: "success",
      comment: "Saved note",
      tags: [],
    },
  ];
  mount(null, "answer", true, true);
  const comment = await screen.findByLabelText("Task review comment");
  await waitFor(() => expect(comment).toHaveValue("Saved note"));
  fail = true;
  fireEvent.change(comment, { target: { value: "Keep this unsaved draft" } });
  await screen.findByRole("button", { name: "Retry save" });
  expect(screen.getByLabelText("Feedback save status")).toHaveTextContent("Not saved");
  expect(comment).toHaveValue("Keep this unsaved draft");
  fail = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry save" }));
  await waitFor(() => expect(outcomes.at(-1)?.comment).toBe("Keep this unsaved draft"));
  await waitFor(() =>
    expect(screen.getByLabelText("Feedback save status")).toHaveTextContent("Saved"),
  );
});
it("retries the initially selected outcome when its first save fails", async () => {
  mount(null, "answer", true, true);
  await waitFor(() => expect(screen.getByRole("button", { name: "Failed" })).toBeEnabled());
  fail = true;
  fireEvent.click(screen.getByRole("button", { name: "Failed" }));
  const retry = await screen.findByRole("button", { name: "Retry save" });
  fail = false;
  fireEvent.click(retry);
  await waitFor(() => expect(outcomes.at(-1)?.outcome).toBe("failed"));
  await waitFor(() =>
    expect(screen.getByLabelText("Feedback save status")).toHaveTextContent("Saved"),
  );
});
it("saves newer comment edits after an older autosave finishes", async () => {
  outcomes = [
    {
      id: "existing",
      kind: "outcome",
      response_id: "answer",
      outcome: "partial",
      comment: "Initial note",
      tags: [],
    },
  ];
  const originalApi = api.getMockImplementation()!;
  let release: (() => void) | undefined;
  let holdFirstWrite = true;
  api.mockImplementation(async (url: string, options?: RequestInit) => {
    if (url.includes("/task-outcomes/") && options?.method === "PUT" && holdFirstWrite) {
      holdFirstWrite = false;
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    }
    return originalApi(url, options);
  });
  mount(null, "answer", true, true);
  const comment = await screen.findByLabelText("Task review comment");
  await waitFor(() => expect(comment).toHaveValue("Initial note"));
  fireEvent.change(comment, { target: { value: "First draft" } });
  await waitFor(() => expect(release).toBeDefined());
  expect(comment).toBeEnabled();
  fireEvent.change(comment, { target: { value: "Latest draft" } });
  release!();
  await waitFor(() => expect(outcomes.at(-1)?.comment).toBe("Latest draft"), { timeout: 2000 });
  expect(comment).toHaveValue("Latest draft");
  await waitFor(() =>
    expect(screen.getByLabelText("Feedback save status")).toHaveTextContent("Saved"),
  );
});
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

it("shows the model used, both choices, the assigned model, rationale, and trace key", async () => {
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
  await waitFor(() => expect(details.textContent).toContain("Advisor’s choice was selected"));
  expect(details.textContent).toContain("Your choice: glm-5.3 · glm · low");
  expect(details.textContent).toContain("Advisor recommendation: gpt-6-sol · openai · high");
  expect(details.textContent).toContain("Assigned model: gpt-6-sol · openai · high");
  expect(details.textContent).toContain(
    "Advisor reasoning: This task needs the stronger reasoning level for a reliable implementation.",
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
  await waitFor(() => expect(details.textContent).toContain("Advisor’s choice was selected"));
  expect(details.textContent).toContain("Your choice: glm-5.3 · glm · low");
  expect(details.textContent).toContain("Advisor recommendation: gpt-6-sol · openai · high");
  expect(details.textContent).toContain(
    "Advisor reasoning: This task needs the stronger reasoning level for a reliable implementation.",
  );
  expect(screen.queryByTestId("human-review-details")).toBeNull();
});

it("says plainly when no advisor recommendation was recorded for the response", async () => {
  advisorRoundAttached = false;
  mount("host-test");
  fireEvent.click(await screen.findByRole("button", { name: "Do not score" }));

  const details = await screen.findByTestId("model-attribution");
  await waitFor(() =>
    expect(details.textContent).toContain("Model Advisor was not used for this response."),
  );
  expect(details.textContent).toContain(
    "Turn on Advisor before sending a message to record its model choice and reasoning.",
  );
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

it("keeps advisor attribution hidden until the response is rated", async () => {
  mount("host-test");
  const rate = await screen.findByRole("button", { name: "Success" });
  expect(screen.queryByTestId("model-attribution")).not.toBeInTheDocument();
  expect(api.mock.calls.some(([url]) => String(url).includes("/model-advisor/rounds/"))).toBe(
    false,
  );
  await waitFor(() => expect(rate).toBeEnabled());
  fireEvent.click(rate);
  const details = await screen.findByTestId("model-attribution");
  await waitFor(() => expect(details).toHaveTextContent("Advisor’s choice was selected"));
});

it("appends accepted AI text without replacing human feedback and autosaves it", async () => {
  outcomes = [
    {
      id: "existing",
      kind: "outcome",
      response_id: "answer",
      outcome: "partial",
      comment: "My observation",
      tags: ["Instructions"],
    },
  ];
  mount(null, "answer", true, true, (review) => (
    <button type="button" onClick={() => review.onAppendComment("Accepted explanation")}>
      Accept AI comment
    </button>
  ));
  await waitFor(() =>
    expect(screen.getByLabelText("Task review comment")).toHaveValue("My observation"),
  );
  fireEvent.click(screen.getByRole("button", { name: "Accept AI comment" }));
  await waitFor(() =>
    expect(outcomes.at(-1)).toMatchObject({
      outcome: "partial",
      comment: "My observation\n\nAccepted explanation",
      tags: ["Instructions"],
    }),
  );
  const count = outcomes.length;
  fireEvent.click(screen.getByRole("button", { name: "Accept AI comment" }));
  expect(screen.getByLabelText("Task review comment")).toHaveValue(
    "My observation\n\nAccepted explanation",
  );
  expect(outcomes).toHaveLength(count);
});
