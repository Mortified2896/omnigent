import { StrictMode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AnswerComparison, type Comparison } from "./AnswerComparison";

const api = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: api, getCurrentUserId: () => "local" }));
vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children: string }) => <div>{children}</div>,
}));
let state: Comparison;
let fail: boolean;
const ready = (): Comparison => ({
  state: "ready",
  candidates: [
    { slot: "a", answer: "First explanation" },
    { slot: "b", answer: "Second explanation" },
  ],
});
beforeEach(() => {
  state = { state: "eligible", candidates: [] };
  fail = false;
  api.mockReset();
  api.mockImplementation(async (_url: string, options?: RequestInit) => {
    if (options?.method === "POST") state = ready();
    if (options?.method === "PUT") {
      if (fail) return Response.json({ detail: "Save failed" }, { status: 500 });
      const vote = JSON.parse(options.body as string);
      state = {
        ...state,
        vote: { ...vote, preferred_arm: vote.choice === "a" ? "human" : "jev" },
        candidates: state.candidates.map((c) => ({
          ...c,
          model: "revealed-model",
          arm: c.slot === "a" ? "human" : "jev",
        })),
      };
    }
    return Response.json(state);
  });
});
afterEach(cleanup);
function mount(autoStart = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <p>Original answer remains available</p>
        <AnswerComparison sessionId="session" responseId="answer" autoStart={autoStart} />
      </QueryClientProvider>
    </StrictMode>,
  );
}
it("automatically generates once, conceals identities, saves and reloads the vote and comment", async () => {
  let view = mount();
  await screen.findByText("Second explanation");
  expect(api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  expect(screen.queryByText(/revealed-model/)).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Why? (optional)"), {
    target: { value: "Clearer reasoning" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Prefer A" }));
  await screen.findByText(/Saved to server/);
  expect(screen.getAllByText(/revealed-model/)).toHaveLength(2);
  view.unmount();
  view = mount();
  expect(await screen.findByLabelText("Why? (optional)")).toHaveValue("Clearer reasoning");
  fireEvent.click(screen.getByRole("button", { name: "Prefer B" }));
  await waitFor(() => expect(state.vote?.choice).toBe("b"));
  expect(state.vote?.comment).toBe("Clearer reasoning");
  fireEvent.change(screen.getByLabelText("Why? (optional)"), { target: { value: "More detail" } });
  expect(screen.getByRole("status")).toHaveTextContent("unsaved");
  fireEvent.click(screen.getByRole("button", { name: "Save comment" }));
  await waitFor(() => expect(state.vote?.comment).toBe("More detail"));
  view.unmount();
});
it("retains the draft after failed saves and only acknowledges a successful retry", async () => {
  state = ready();
  mount();
  fireEvent.change(await screen.findByLabelText("Why? (optional)"), {
    target: { value: "Keep this draft" },
  });
  fail = true;
  fireEvent.click(screen.getByRole("button", { name: "Prefer A" }));
  await screen.findByRole("alert");
  expect(screen.queryByText(/Saved to server/)).not.toBeInTheDocument();
  expect(screen.getByLabelText("Why? (optional)")).toHaveValue("Keep this draft");
  fail = false;
  fireEvent.click(screen.getByRole("button", { name: "Prefer A" }));
  await screen.findByText(/Saved to server/);
});
it("does not start historical comparisons and keeps pending or failed comparison independent of the original", async () => {
  const view = mount(false);
  fireEvent.click(await screen.findByRole("button", { name: "Collapse comparison" }));
  expect(screen.getByRole("button", { name: "Show comparison" })).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  expect(api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
  view.unmount();
  state = { state: "failed", candidates: [], error: "Comparison interrupted." };
  mount();
  await screen.findByText(/Comparison interrupted/);
  expect(screen.getByText("Original answer remains available")).toBeVisible();
  expect(api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
});
