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
const prepare = vi.fn();
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
    prepare,
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
it("prepares a composer review without sending or creating a branch", () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Feedback discussion" }));
  expect(prepare).toHaveBeenCalledWith(
    expect.objectContaining({
      responseId: "answer",
      intent: "discuss",
      answerExcerpt: "The selected older answer",
      original: expect.objectContaining({ outcome: "partial" }),
    }),
  );
  expect(mocks.send).not.toHaveBeenCalled();
  expect(mocks.forkSession).not.toHaveBeenCalled();
  expect(review.onReplaceDetails).not.toHaveBeenCalled();
});
it("prepares Self Reflection as a separate intent", () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Self Reflection" }));
  expect(prepare).toHaveBeenCalledWith(expect.objectContaining({ intent: "inspect" }));
  expect(mocks.send).not.toHaveBeenCalled();
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
  expect(screen.getByRole("button", { name: "Feedback discussion" })).toBeDisabled();
  cleanup();
  mocks.state.status = "streaming";
  mount();
  expect(screen.getByRole("button", { name: "Feedback discussion" })).toBeDisabled();

  expect(screen.getByRole("button", { name: "Open side chat" })).not.toBeDisabled();
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
