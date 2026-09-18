import { createContext, useContext, useId, useMemo, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  useResponseFeedback,
  useSaveResponseFeedback,
  type ResponseFeedback,
} from "@/hooks/useResponseFeedback";
import type { Bubble } from "@/lib/renderItems";
import { LIVE_ITEM_PREFIX } from "@/lib/blocks";

const FeedbackContext = createContext<{
  sessionId: string;
  rows: Map<string, ResponseFeedback>;
  ready: boolean;
} | null>(null);

export function ResponseFeedbackProvider({
  sessionId,
  children,
}: {
  sessionId: string;
  children: React.ReactNode;
}) {
  const query = useResponseFeedback(sessionId);
  const value = useMemo(
    () => ({
      sessionId,
      rows: new Map((query.data ?? []).map((row) => [row.response_id, row])),
      ready: query.isSuccess,
    }),
    [sessionId, query.data, query.isSuccess],
  );
  return <FeedbackContext.Provider value={value}>{children}</FeedbackContext.Provider>;
}

export function canRateResponse(bubble: Bubble): boolean {
  return (
    bubble.kind === "assistant" &&
    bubble.lifecycle === "completed" &&
    !bubble.continued &&
    bubble.items.some(
      (item) =>
        item.kind === "text" &&
        item.final &&
        item.text.trim() &&
        item.itemId &&
        !item.itemId.startsWith(LIVE_ITEM_PREFIX),
    )
  );
}

export function ResponseFeedbackActions({ responseId }: { responseId: string }) {
  const context = useContext(FeedbackContext);
  if (!context) return null;
  return (
    <FeedbackEditor
      key={`${context.sessionId}:${responseId}`}
      sessionId={context.sessionId}
      responseId={responseId}
      feedback={context.rows.get(responseId)}
      ready={context.ready}
    />
  );
}

function FeedbackEditor({
  sessionId,
  responseId,
  feedback,
  ready,
}: {
  sessionId: string;
  responseId: string;
  feedback?: ResponseFeedback;
  ready: boolean;
}) {
  const mutation = useSaveResponseFeedback(sessionId, responseId);
  const [draft, setDraft] = useState<string | null>(null);
  const id = useId();
  const comment = draft ?? feedback?.comment ?? "";
  const disabled = !ready || mutation.isPending;
  return (
    <div className="contents" aria-label="Response feedback">
      {([1, -1] as const).map((rating) => (
        <Button
          key={rating}
          type="button"
          size="icon-sm"
          variant={feedback?.rating === rating ? "secondary" : "ghost"}
          className="min-h-10 min-w-10 md:min-h-7 md:min-w-7"
          aria-label={rating === 1 ? "Good response" : "Bad response"}
          aria-pressed={feedback?.rating === rating}
          disabled={disabled}
          onClick={() => mutation.mutate({ rating })}
        >
          {rating === 1 ? (
            <ThumbsUp aria-hidden="true" size={14} />
          ) : (
            <ThumbsDown aria-hidden="true" size={14} />
          )}
        </Button>
      ))}
      {feedback && (
        <div className="order-last flex w-full min-w-0 basis-full flex-wrap items-end gap-2">
          <label htmlFor={id} className="w-full text-xs">
            Why was this {feedback.rating === 1 ? "good" : "bad"}? (optional)
          </label>
          <textarea
            id={id}
            value={comment}
            maxLength={4000}
            rows={2}
            disabled={mutation.isPending}
            className="min-w-0 w-full rounded-md border bg-background p-2 text-sm"
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button
            type="button"
            size="sm"
            disabled={disabled || comment === (feedback.comment ?? "")}
            onClick={() =>
              mutation.mutate(
                { rating: feedback.rating, comment: comment || null },
                { onSuccess: () => setDraft(null) },
              )
            }
          >
            Save comment
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={disabled}
            onClick={() => mutation.mutate(null, { onSuccess: () => setDraft(null) })}
          >
            Clear feedback
          </Button>
        </div>
      )}
      {mutation.isPending && (
        <span role="status" className="text-xs">
          Saving…
        </span>
      )}
      {mutation.isError && (
        <span role="alert" className="order-last w-full text-xs text-destructive">
          Feedback was not saved. Please try again.
        </span>
      )}
      {!ready && (
        <span role="status" className="text-xs">
          Feedback unavailable
        </span>
      )}
    </div>
  );
}
