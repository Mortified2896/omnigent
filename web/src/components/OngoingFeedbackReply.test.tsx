import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { OngoingFeedbackReply } from "./OngoingFeedbackReply";
import { FeedbackDiscussionContext, type ReviewPerspectiveInput } from "./FeedbackContext";
import { useMemo } from "react";

const vote = vi.hoisted(() => vi.fn());
const prepare = vi.fn();
vi.mock("@/hooks/useResponseFeedback", () => ({
  useResponseFeedback: () => ({ data: [] }),
  useSaveResponseFeedback: () => ({ mutate: vote, isError: false }),
}));
afterEach(() => {
  cleanup();
  localStorage.clear();
  vote.mockClear();
  prepare.mockClear();
});
const target = {
  responseId: "original",
  intent: "discuss" as const,
  original: { outcome: "failed", comment: "Needs testing", tags: ["Environment"] },
};
const text =
  'Consider Partial.\n```feedback-json\n{"outcome":"partial","comment":"Local checks passed","tags":["Testing"]}\n```';
function mount(saved: boolean) {
  const apply = vi.fn().mockResolvedValue(undefined);
  const review: ReviewPerspectiveInput = {
    ...target.original,
    outcome: "failed",
    saved,
    onAddTag: vi.fn(),
    onAppendComment: vi.fn(),
    onReplaceDetails: apply,
  };
  return { apply, view: render(<ReplyHarness review={review} />) };
}
function ReplyHarness({ review }: { review: ReviewPerspectiveInput }) {
  const context = useMemo(
    () => ({
      sessionId: "session",
      prepare,
      threads: [],
      active: null,
      setActive: vi.fn(),
      wide: true,
      currentChatSendNonce: 0,
      onCurrentChatSend: vi.fn(),
      reviews: { current: new Map([["original", review]]) },
      renderTranscript: () => null,
    }),
    [review],
  );
  return (
    <FeedbackDiscussionContext.Provider value={context}>
      <OngoingFeedbackReply
        sessionId="session"
        responseId="discussion-reply"
        target={target}
        text={text}
      />
    </FeedbackDiscussionContext.Provider>
  );
}
it("applies a proposal to the selected original response only after acceptance", async () => {
  const { apply } = mount(true);
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Accept changes" }));
  await waitFor(() =>
    expect(apply).toHaveBeenCalledWith({
      outcome: "partial",
      comment: "Local checks passed",
      tags: ["Testing"],
    }),
  );
});
it("keeps unsaved authored feedback safe and votes on the discussion reply independently", async () => {
  const { apply } = mount(false);
  fireEvent.click(screen.getByRole("button", { name: "Thumbs up feedback reply" }));
  expect(vote).toHaveBeenCalledWith({ rating: 1 });
  fireEvent.click(screen.getByRole("button", { name: "Accept changes" }));
  await screen.findByText("Wait for your current feedback to save before applying changes.");
  expect(apply).not.toHaveBeenCalled();
});
it("prepares a review follow-up for the same original answer", () => {
  const { apply } = mount(true);
  fireEvent.click(screen.getByRole("button", { name: "Review feedback" }));
  expect(prepare).toHaveBeenCalledWith(
    expect.objectContaining({
      responseId: "original",
      intent: "suggest",
      original: target.original,
    }),
  );
  expect(apply).not.toHaveBeenCalled();
});
