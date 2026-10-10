import { useContext, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { SparklesIcon } from "lucide-react";
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
  const retry = useRef<{ text: string; stableId: string } | null>(null);
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
  const discussHere = async () => {
    if (!review.saved || busy || sending.current || streaming) return;
    sending.current = true;
    setBusy(true);
    setError(null);
    const sessionId = context.sessionId;
    const text =
      INITIAL_QUESTION +
      CONTEXT_MARKER +
      "Discuss the selected answer and my feedback without tools or project changes. " +
      "Use the excerpt to locate the full answer in this conversation. " +
      "My outcome is authoritative. Suggest improvements in prose; do not change saved feedback.\n" +
      JSON.stringify({
        response_id: responseId,
        selected_answer_excerpt: answerText?.slice(0, 2000),
        outcome: review.outcome,
        comment: review.comment,
        tags: review.tags,
      });
    try {
      const source = await getSession(sessionId);
      const live = useChatStore.getState();
      if (
        ["running", "launching", "waiting"].includes(source.status) ||
        source.pendingInputs?.length ||
        (live.conversationId === sessionId && live.status === "streaming")
      )
        throw new Error("Wait for the current response to finish before discussing feedback here.");
      if (!source.agentId) throw new Error("Reconnect this chat to discuss feedback.");
      if (retry.current?.text !== text)
        retry.current = { text, stableId: crypto.randomUUID().replaceAll("-", "") };
      let failed = false;
      await useChatStore.getState().send(text, source.agentId, undefined, {
        pinnedConversationId: sessionId,
        stableId: retry.current.stableId,
        onError: (message) => {
          failed = true;
          setError(message);
        },
      });
      if (!failed) {
        retry.current = null;
        if (useChatStore.getState().conversationId === sessionId) context.onCurrentChatSend();
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start feedback discussion");
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };
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
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-muted/20 p-3">
        <div>
          <p className="flex items-center gap-2 text-sm font-medium">
            <SparklesIcon className="size-4" />
            AI perspective
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            Continue here with this chat’s context. Your feedback stays authoritative.
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!review.saved || busy || streaming}
          onClick={() => void discussHere()}
        >
          {busy ? "Opening…" : "Discuss in this chat"}
        </Button>
        {streaming && (
          <p className="w-full text-xs text-muted-foreground">
            Available when the current response finishes.
          </p>
        )}
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
