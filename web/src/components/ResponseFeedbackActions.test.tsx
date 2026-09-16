import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  ResponseFeedbackActions,
  ResponseFeedbackProvider,
  canRateResponse,
} from "./ResponseFeedbackActions";
import type { ResponseFeedback } from "@/hooks/useResponseFeedback";
import type { Bubble } from "@/lib/renderItems";

const api = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: api, getCurrentUserId: () => "local" }));
let stored: ResponseFeedback[];
let fail: boolean;
beforeEach(() => {
  stored = [];
  fail = false;
  api.mockReset();
  api.mockImplementation(async (_url: string, options?: RequestInit) => {
    if (options?.method && fail) return new Response(null, { status: 500 });
    if (options?.method === "DELETE") {
      stored = [];
      return new Response(null, { status: 204 });
    }
    if (options?.method === "PUT") {
      stored = [
        {
          conversation_id: "session",
          response_id: "answer",
          comment: null,
          created_at: 1,
          updated_at: 2,
          ...(stored.at(0) ?? {}),
          ...JSON.parse(options.body as string),
        },
      ];
      return Response.json(stored[0]);
    }
    return Response.json(stored);
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
it("round trips rating, comment edits, polarity and clear across a fresh query cache", async () => {
  const view = mount();
  const good = await screen.findByRole("button", { name: "Good response" });
  await waitFor(() => expect(good).toBeEnabled());
  good.focus();
  expect(document.activeElement).toBe(good);
  expect(good.tagName).toBe("BUTTON");
  fireEvent.click(good);
  await waitFor(() => expect(good).toHaveAttribute("aria-pressed", "true"));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Useful detail" } });
  expect(api.mock.calls.filter((call) => call[1]?.method === "PUT")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Save comment" }));
  await waitFor(() => expect(stored[0]?.comment).toBe("Useful detail"));
  view.unmount();
  mount();
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("Useful detail"));
  fireEvent.click(screen.getByRole("button", { name: "Bad response" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Bad response" })).toHaveAttribute(
      "aria-pressed",
      "true",
    ),
  );
  expect(stored).toHaveLength(1);
  expect(stored[0]?.comment).toBe("Useful detail");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Revised" } });
  fireEvent.click(screen.getByRole("button", { name: "Save comment" }));
  await waitFor(() => expect(stored[0]?.comment).toBe("Revised"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Clear feedback" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Clear feedback" }));
  await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
});
it("keeps confirmed state on failed rating, comment and delete", async () => {
  stored = [
    {
      conversation_id: "session",
      response_id: "answer",
      rating: 1,
      comment: "Saved",
      created_at: 1,
      updated_at: 1,
    },
  ];
  mount();
  await screen.findByRole("textbox");
  fail = true;
  fireEvent.click(screen.getByRole("button", { name: "Bad response" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Good response" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Unsaved draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Save comment" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Save comment" })).toBeEnabled());
  expect(stored[0]?.comment).toBe("Saved");
  expect(screen.getByRole("textbox")).toHaveValue("Unsaved draft");
  fireEvent.click(screen.getByRole("button", { name: "Clear feedback" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Clear feedback" })).toBeEnabled());
  expect(stored).toHaveLength(1);
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
