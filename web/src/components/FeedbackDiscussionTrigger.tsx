import { useContext, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { FeedbackDiscussionControls } from "./FeedbackDiscussionControls";
import { getCurrentUserId } from "@/lib/identity";
import {
  fetchSessionItemsPage,
  forkSession,
  getSession,
  launchRunner,
  updateSession,
} from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { Button } from "@/components/ui/button";
import {
  FeedbackDiscussionContext as Context,
  readOriginal,
  type Original,
  type Thread,
  type ReviewPerspectiveInput,
} from "./FeedbackContext";

const INITIAL_QUESTION =
  "Please give your AI perspective on my feedback and suggest any changes you think would make it more accurate.";
const CONTEXT_MARKER = "\n\nFeedback review context:\n";
function initialQuestion(original: Original): string {
  // The native fork may clone its vendor transcript instead of rebuilding
  // from Omnigent items. Send the snapshot explicitly on this branch's first
  // turn so both paths receive it; the parent chat never receives this input.
  return (
    INITIAL_QUESTION +
    CONTEXT_MARKER +
    "My outcome is authoritative. Discuss your perspective without tools or project changes. " +
    'If suggesting edits, include a fenced feedback-json block containing {"outcome": "partial", "comment": "...", "tags": ["..."]}. ' +
    "You may suggest success, partial, failed, or not_sure with reasons; every edit needs my explicit acceptance. Limit comment to 4000 characters and tags to 8 of at most 64 characters.\n" +
    JSON.stringify(original)
  );
}
export function FeedbackDiscussion({
  responseId,
  review,
  answerText,
}: {
  responseId: string;
  review: ReviewPerspectiveInput;
  answerText?: string;
}) {
  const context = useContext(Context);
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [branchId, setBranchId] = useState<string | null>(null);
  const sending = useRef(false);
  const streaming = useChatStore(
    (state) => state.conversationId === context?.sessionId && state.status === "streaming",
  );
  const thread = context?.threads.filter((t) => t.response_id === responseId).at(-1);
  const existingId = branchId ?? thread?.session_id;
  useEffect(() => {
    const reviews = context?.reviews.current;
    reviews?.set(responseId, review);
    return () => {
      if (reviews?.get(responseId) === review) reviews.delete(responseId);
    };
  }, [context, responseId, review]);
  if (!context) return null;
  const start = async () => {
    if (!review.saved || busy || sending.current) return;
    sending.current = true;
    setBusy(true);
    setError(null);
    try {
      const source = await getSession(context.sessionId);
      let fork = existingId
        ? await getSession(existingId)
        : await forkSession(context.sessionId, { feedbackResponseId: responseId });
      setBranchId(fork.id);
      if (!fork.runnerId || fork.runnerOnline === false) {
        if (source.hostId && source.workspace && source.hostOnline !== false)
          await launchRunner(source.hostId, fork.id, source.workspace);
        else if (source.runnerId) await updateSession(fork.id, { runnerId: source.runnerId });
        else throw new Error("Reconnect the original session to start the discussion");
        fork = await getSession(fork.id);
      }
      await client.invalidateQueries({
        queryKey: ["feedback-discussions", getCurrentUserId(), context.sessionId],
      });
      context.setActive({ responseId, branchId: fork.id });
      const linked = client
        .getQueryData<Thread[]>(["feedback-discussions", getCurrentUserId(), context.sessionId])
        ?.find((t) => t.session_id === fork.id);
      const inherited = new Set(linked?.inherited_ids ?? []);
      const page = await fetchSessionItemsPage(fork.id);
      const hasQuestion = page.items.some(
        (item) =>
          !inherited.has(item.id) &&
          item.type === "message" &&
          item.role === "user" &&
          !item.is_meta,
      );
      // Also check server-pending input so reconnecting during a native paste
      // waits for the original message rather than starting a second turn.
      if (fork.agentId && !hasQuestion && !fork.pendingInputs?.length) {
        const original = readOriginal(fork, linked);
        if (!original) throw new Error("Original feedback is unavailable");
        const digest = await crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(`feedback-initial:${fork.id}`),
        );
        const stableId = Array.from(new Uint8Array(digest))
          .map((byte) => byte.toString(16).padStart(2, "0"))
          .join("")
          .slice(0, 32);
        await useChatStore.getState().send(initialQuestion(original), fork.agentId, undefined, {
          pinnedConversationId: fork.id,
          stableId,
          onError: (message) => setError(message),
        });
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start feedback discussion");
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };
  const open = context.active?.responseId === responseId;
  return (
    <>
      <div className="space-y-2">
        <FeedbackDiscussionControls
          variant="footer"
          busy={busy || streaming}
          saving={!review.saved}
          onPrepare={(intent) =>
            context.prepare?.({
              id: crypto.randomUUID(),
              responseId,
              intent,
              original: {
                outcome: review.outcome,
                comment: review.comment,
                tags: [...review.tags],
              },
              answerExcerpt: answerText?.slice(0, 2000),
            })
          }
          onSubmitInline={() => {}}
        />
        <details className="w-full">
          <summary className="cursor-pointer text-xs text-muted-foreground">
            Advanced options
          </summary>
          <p className="my-2 text-xs text-muted-foreground">
            Use a separate discussion to keep this chat focused.
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!review.saved || busy}
            onClick={() => void start()}
          >
            {existingId ? "Reopen side chat" : "Open side chat"}
          </Button>
        </details>
        {error && (
          <p role="alert" className="w-full text-sm text-destructive">
            {error}
          </p>
        )}
      </div>
      {!context.wide && open && context.active && context.renderTranscript(context.active)}
    </>
  );
}
