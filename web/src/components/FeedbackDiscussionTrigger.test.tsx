import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FeedbackDiscussionContext, type ReviewPerspectiveInput } from "./FeedbackContext";
import { FeedbackDiscussion } from "./FeedbackDiscussionTrigger";

const mocks = vi.hoisted(() => ({
  getSession: vi.fn(),
  forkSession: vi.fn(),
  launchRunner: vi.fn(),
  updateSession: vi.fn(),
  fetchSessionItemsPage: vi.fn(),
  send: vi.fn(),
  state: { conversationId: "parent", status: "idle" },
}));
vi.mock("@/lib/sessionsApi", () => mocks);
vi.mock("@/lib/identity", () => ({ getCurrentUserId: () => "owner" }));
vi.mock("@/store/chatStore", () => ({
  useChatStore: Object.assign((selector: (state: unknown) => unknown) => selector(mocks.state), {
    getState: () => ({ ...mocks.state, send: mocks.send }),
  }),
}));
const review: ReviewPerspectiveInput = {
  outcome: "partial",
  comment: "Needs verification",
  tags: ["Tests/verification"],
  saved: true,
  onAddTag: vi.fn(),
  onAppendComment: vi.fn(),
  onReplaceDetails: vi.fn(),
};
const scroll = vi.fn();
const activate = vi.fn();
function discussionContext(
  threads: { session_id: string; response_id: string; inherited_ids: string[] }[],
) {
  return {
    sessionId: "parent",
    threads,
    active: null,
    setActive: activate,
    wide: true,
    reviews: { current: new Map() },
    renderTranscript: () => null,
    currentChatSendNonce: 0,
    onCurrentChatSend: scroll,
  };
}
function mount(options: { saved?: boolean; existing?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const threads = options.existing
    ? [{ session_id: "branch", response_id: "answer", inherited_ids: [] }]
    : [];
  client.setQueryData(["feedback-discussions", "owner", "parent"], threads);
  const value = discussionContext(threads);
  return render(
    <QueryClientProvider client={client}>
      <FeedbackDiscussionContext.Provider value={value}>
        <FeedbackDiscussion
          responseId="answer"
          answerText="The selected older answer"
          review={{ ...review, saved: options.saved ?? true }}
        />
      </FeedbackDiscussionContext.Provider>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.state = { conversationId: "parent", status: "idle" };
  mocks.getSession.mockResolvedValue({
    id: "parent",
    agentId: "agent",
    status: "idle",
    runnerId: "runner",
  });
  mocks.send.mockResolvedValue(undefined);
});
afterEach(cleanup);
it("defaults to the ongoing chat and appends selected feedback without creating a branch", async () => {
  mount();
  expect(screen.getByText("Advanced options").parentElement).not.toHaveAttribute("open");
  fireEvent.click(screen.getByRole("button", { name: "Discuss in this chat" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
  const [text, agent, files, options] = mocks.send.mock.calls[0];
  expect(text).toContain('"selected_answer_excerpt":"The selected older answer"');
  expect(text).toContain('"response_id":"answer"');
  expect(text).toContain('"outcome":"partial"');
  expect(text).toContain("do not change saved feedback");
  expect(agent).toBe("agent");
  expect(files).toBeUndefined();
  expect(options).toMatchObject({ pinnedConversationId: "parent", stableId: expect.any(String) });
  expect(mocks.forkSession).not.toHaveBeenCalled();
  expect(mocks.launchRunner).not.toHaveBeenCalled();
  expect(mocks.updateSession).not.toHaveBeenCalled();
  expect(review.onReplaceDetails).not.toHaveBeenCalled();
  expect(scroll).toHaveBeenCalledOnce();
});
it("reopens an existing side chat through Advanced options without duplicating its question", async () => {
  mocks.getSession.mockImplementation(async (id: string) => ({
    id,
    agentId: "agent",
    status: "idle",
    runnerId: "runner",
  }));
  mocks.fetchSessionItemsPage.mockResolvedValue({
    items: [{ id: "question", type: "message", role: "user" }],
  });
  mount({ existing: true });
  fireEvent.click(screen.getByText("Advanced options"));
  fireEvent.click(screen.getByRole("button", { name: "Reopen side chat" }));
  await waitFor(() =>
    expect(activate).toHaveBeenCalledWith({ responseId: "answer", branchId: "branch" }),
  );
  expect(mocks.forkSession).not.toHaveBeenCalled();
  expect(mocks.send).not.toHaveBeenCalled();
});
it("keeps unsaved feedback and a running response from starting an ongoing discussion", () => {
  mount({ saved: false });
  expect(screen.getByRole("button", { name: "Discuss in this chat" })).toBeDisabled();
  cleanup();
  mocks.state.status = "streaming";
  mount();
  expect(screen.getByRole("button", { name: "Discuss in this chat" })).toBeDisabled();
  expect(screen.getByText("Available when the current response finishes.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open side chat" })).not.toBeDisabled();
});
it("checks fresh server state to avoid injecting feedback into an in-flight response", async () => {
  mocks.getSession.mockResolvedValue({ id: "parent", agentId: "agent", status: "running" });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Discuss in this chat" }));
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent("Wait for the current response"),
  );
  expect(mocks.send).not.toHaveBeenCalled();
});
it("retains an idempotent request for a failed retry and ignores rapid double clicks", async () => {
  mocks.send.mockImplementationOnce(async (_text, _agent, _files, options) =>
    options.onError("Connection lost"),
  );
  mount();
  const button = screen.getByRole("button", { name: "Discuss in this chat" });
  fireEvent.click(button);
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Connection lost"));
  expect(mocks.send).toHaveBeenCalledOnce();
  expect(scroll).not.toHaveBeenCalled();
  fireEvent.click(button);
  await waitFor(() => expect(mocks.send).toHaveBeenCalledTimes(2));
  expect(mocks.send.mock.calls[1][3].stableId).toBe(mocks.send.mock.calls[0][3].stableId);
});
it("creates a saved side discussion only when the advanced action is selected", async () => {
  mocks.forkSession.mockResolvedValue({
    id: "new-branch",
    agentId: "agent",
    runnerId: "runner",
    status: "idle",
    labels: {
      "omnigent.feedback.original": JSON.stringify({
        outcome: "partial",
        comment: "Needs verification",
        tags: [],
      }),
    },
  });
  mocks.fetchSessionItemsPage.mockResolvedValue({ items: [] });
  mount();
  fireEvent.click(screen.getByText("Advanced options"));
  fireEvent.click(screen.getByRole("button", { name: "Open side chat" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
  expect(mocks.forkSession).toHaveBeenCalledWith("parent", { feedbackResponseId: "answer" });
  expect(mocks.send.mock.calls[0][3]).toMatchObject({ pinnedConversationId: "new-branch" });
  expect(mocks.send.mock.calls[0][0]).toContain("feedback-json");
  expect(scroll).not.toHaveBeenCalled();
});
it("pins the ongoing request to its original chat if navigation happens during the session lookup", async () => {
  mocks.getSession.mockImplementation(async () => {
    mocks.state.conversationId = "another-chat";
    return { id: "parent", agentId: "agent", status: "idle" };
  });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Discuss in this chat" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
  expect(mocks.send.mock.calls[0][3].pinnedConversationId).toBe("parent");
  expect(scroll).not.toHaveBeenCalled();
});
