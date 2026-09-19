import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ResponseFeedbackActions, ResponseFeedbackProvider } from "./ResponseFeedbackActions";
import type { SessionScoringPolicy } from "@/hooks/useScoringEligibility";

const api = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: api, getCurrentUserId: () => "local" }));
let policy: SessionScoringPolicy;
let outcomes: Record<string, unknown>[];
let fail: boolean;
beforeEach(() => {
  policy = { score_eligible: true, is_test: false, retention: null, responses: {} };
  outcomes = [];
  fail = false;
  api.mockReset();
  api.mockImplementation(async (url: string, options?: RequestInit) => {
    if (url.endsWith("/scoring-policy")) return Response.json(policy);
    if (url.endsWith("/task-experiment")) return Response.json(outcomes);
    if (options?.method && fail) return new Response(null, { status: 500 });
    if (url.includes("/scoring-eligibility/")) {
      const data = JSON.parse(options!.body as string);
      policy = { ...policy, responses: { answer: data } };
      return Response.json(data);
    }
    if (url.includes("/task-outcomes/")) {
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
    throw new Error(`Unexpected API call: ${url}`);
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

it("excludes an unrated response, persists the reason across reload, and restores inclusion", async () => {
  let view = mount();
  fireEvent.click(await screen.findByRole("button", { name: "Do not score" }));
  await screen.findByLabelText("Scoring exclusion reason");
  expect(outcomes).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("Scoring exclusion reason"), {
    target: { value: "test_fixture" },
  });
  await waitFor(() => expect(policy.responses.answer.exclusion_reason).toBe("test_fixture"));
  view.unmount();
  view = mount();
  const exclude = await screen.findByRole("button", { name: "Do not score" });
  expect(exclude).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByLabelText("Scoring exclusion reason")).toHaveValue("test_fixture");
  fireEvent.click(exclude);
  await waitFor(() => expect(policy.responses.answer.score_eligible).toBe(true));
  expect(outcomes).toHaveLength(0);
  view.unmount();
});

it("preserves an unsaved human comment and tags while toggling exclusion", async () => {
  outcomes = [
    {
      id: "original",
      kind: "outcome",
      response_id: "answer",
      outcome: "partial",
      comment: "",
      tags: [],
    },
  ];
  mount();
  const comment = await screen.findByLabelText("Task review comment");
  fireEvent.change(comment, { target: { value: "My unsaved note" } });
  fireEvent.click(screen.getByRole("button", { name: "Tests/verification" }));
  fireEvent.click(await screen.findByRole("button", { name: "Do not score" }));
  await waitFor(() => expect(policy.responses.answer.score_eligible).toBe(false));
  expect(comment).toHaveValue("My unsaved note");
  expect(screen.getByRole("button", { name: "Tests/verification" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: "Success" }));
  await waitFor(() =>
    expect(outcomes.at(-1)).toMatchObject({
      outcome: "success",
      comment: "My unsaved note",
      tags: ["Tests/verification"],
    }),
  );
  expect(policy.responses.answer.score_eligible).toBe(false);
});

it("keeps the previous confirmed setting after a failed mutation", async () => {
  policy.responses.answer = { score_eligible: false, exclusion_reason: "duplicate" };
  mount();
  const exclude = await screen.findByRole("button", { name: "Do not score" });
  fail = true;
  fireEvent.click(exclude);
  await screen.findByRole("alert");
  expect(exclude).toHaveAttribute("aria-pressed", "true");
  expect(policy.responses.answer.score_eligible).toBe(false);
});

it("shows retained test evidence and refuses per-response re-inclusion", async () => {
  policy = {
    score_eligible: false,
    is_test: true,
    retention: "keep_for_inspection",
    responses: {},
  };
  mount();
  expect(await screen.findByRole("status")).toHaveTextContent("Kept for inspection");
  const exclude = screen.getByRole("button", { name: "Do not score" });
  expect(exclude).toBeDisabled();
  expect(exclude).toHaveAttribute("aria-pressed", "true");
});
