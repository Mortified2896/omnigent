import { useContext } from "react";
import { FeedbackDiscussionContext } from "./FeedbackContext";
import { parseFeedbackProposal } from "@/lib/feedbackProposal";
import { SuggestedFeedback } from "./SuggestedFeedback";
import { FeedbackReplyRating } from "./FeedbackReplyRating";
import { useResponseFeedback, useSaveResponseFeedback } from "@/hooks/useResponseFeedback";
import type { FeedbackTarget } from "@/lib/feedbackPrompts";
import { getCurrentUserId } from "@/lib/identity";
import { Button } from "@/components/ui/button";

export function OngoingFeedbackActions({
  sessionId,
  responseId,
  target,
}: {
  sessionId: string;
  responseId: string;
  target: FeedbackTarget;
}) {
  const context = useContext(FeedbackDiscussionContext);
  const feedback = useResponseFeedback(sessionId);
  const saveVote = useSaveResponseFeedback(sessionId, responseId);
  const vote = feedback.data?.find((row) => row.response_id === responseId)?.rating;
  const review = context?.reviews.current.get(target.responseId);
  return (
    <>
      <FeedbackReplyRating
        value={vote === 1 ? "up" : vote === -1 ? "down" : null}
        onChange={(value) => saveVote.mutate(value ? { rating: value === "up" ? 1 : -1 } : null)}
      />
      {target.intent === "discuss" && (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="min-h-10 px-0 text-muted-foreground md:min-h-0"
          disabled={!review?.saved || !context?.prepare}
          onClick={() =>
            context?.prepare?.({
              id: crypto.randomUUID(),
              ...target,
              intent: "suggest",
              original: review
                ? { outcome: review.outcome, comment: review.comment, tags: [...review.tags] }
                : target.original,
            })
          }
        >
          Review feedback
        </Button>
      )}
      {saveVote.isError && (
        <p role="alert" className="text-xs text-destructive">
          Reply vote was not saved. Please try again.
        </p>
      )}
    </>
  );
}

/** Review votes and edits save through the existing authenticated feedback APIs. */
export function OngoingFeedbackReply({
  sessionId,
  responseId,
  target,
  text,
  showActions = true,
}: {
  sessionId: string;
  responseId: string;
  target: FeedbackTarget;
  text: string;
  showActions?: boolean;
}) {
  const context = useContext(FeedbackDiscussionContext);
  const proposal = parseFeedbackProposal(text);
  return (
    <div className="w-full space-y-2">
      {showActions && (
        <div className="flex flex-wrap items-center gap-2">
          <OngoingFeedbackActions sessionId={sessionId} responseId={responseId} target={target} />
        </div>
      )}
      {proposal && (
        <SuggestedFeedback
          id={`${getCurrentUserId()}:${sessionId}:${responseId}`}
          original={target.original}
          proposed={proposal}
          onApply={async (details) => {
            const review = context?.reviews.current.get(target.responseId);
            if (!review)
              throw new Error("Open the original answer's feedback to apply these changes.");
            if (!review.saved)
              throw new Error("Wait for your current feedback to save before applying changes.");
            await review.onReplaceDetails(details);
          }}
        />
      )}
    </div>
  );
}
