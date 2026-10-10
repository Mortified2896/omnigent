import {
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { XIcon } from "lucide-react";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";
import { getSession } from "@/lib/sessionsApi";
import { useTaskExperiment, type TaskOutcome } from "@/hooks/useTaskExperiment";
import type { Bubble } from "@/lib/renderItems";
import { Button } from "@/components/ui/button";
import { SideChatPane } from "./chat/SideChatPane";
import {
  FeedbackDisabled,
  FeedbackDiscussionContext as Context,
  readOriginal,
  type Active,
  type Thread,
  type ReviewPerspectiveInput,
} from "./FeedbackContext";
import { SuggestedFeedback } from "./SuggestedFeedback";

const CONTEXT_MARKER = "\n\nFeedback review context:\n";

export function FeedbackDiscussionsProvider({
  sessionId,
  children,
}: {
  sessionId: string;
  children: ReactNode;
}) {
  const [active, setActive] = useState<Active | null>(null);
  const [currentChatSendNonce, setCurrentChatSendNonce] = useState(0);
  const onCurrentChatSend = useCallback(() => setCurrentChatSendNonce((nonce) => nonce + 1), []);
  const [wide, setWide] = useState(() => window.matchMedia("(min-width: 1280px)").matches);
  const reviews = useRef(new Map<string, ReviewPerspectiveInput>());
  const threads = useQuery({
    queryKey: ["feedback-discussions", getCurrentUserId(), sessionId],
    queryFn: async () => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/feedback-discussions`,
      );
      if (!response.ok) throw new Error("Could not load feedback discussions");
      return (await response.json()) as Thread[];
    },
  });
  useEffect(() => {
    setActive(null);
    setCurrentChatSendNonce(0);
    reviews.current.clear();
  }, [sessionId]);
  useEffect(() => {
    const media = window.matchMedia("(min-width: 1280px)");
    const change = () => setWide(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  const value = useMemo(
    () => ({
      sessionId,
      threads: threads.data ?? [],
      active,
      setActive,
      wide,
      currentChatSendNonce,
      onCurrentChatSend,
      reviews,
      renderTranscript: (current: Active) => (
        <FeedbackTranscript key={current.branchId} active={current} />
      ),
    }),
    [sessionId, threads.data, active, wide, currentChatSendNonce, onCurrentChatSend],
  );
  return (
    <Context.Provider value={value}>
      <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
        {children}
        {wide && active && (
          <aside
            className="flex w-[380px] min-h-0 shrink-0 flex-col border-l bg-background pt-12"
            aria-label="Feedback discussion side chat"
          >
            <FeedbackTranscript key={active.branchId} active={active} />
          </aside>
        )}
      </div>
    </Context.Provider>
  );
}

export function parseFeedbackProposal(
  text: string,
): { comment: string; tags: string[]; outcome?: TaskOutcome } | null {
  const match = text.match(/```feedback-json\s*([\s\S]*?)```/);
  if (!match) return null;
  try {
    const value = JSON.parse(match[1]);
    if (
      !value ||
      (value.outcome !== undefined &&
        !["success", "partial", "failed", "not_sure"].includes(value.outcome)) ||
      typeof value.comment !== "string" ||
      value.comment.length > 4000 ||
      !Array.isArray(value.tags) ||
      value.tags.length > 8 ||
      value.tags.some((tag: unknown) => typeof tag !== "string" || !tag.trim() || tag.length > 64)
    )
      return null;
    return {
      ...(value.outcome === undefined ? {} : { outcome: value.outcome as TaskOutcome }),
      comment: value.comment,
      tags: [...new Set<string>(value.tags.map((tag: string) => tag.trim()))],
    };
  } catch {
    return null;
  }
}

function FeedbackTranscript({ active }: { active: Active }) {
  const context = useContext(Context)!;
  const branch = useQuery({
    queryKey: ["session", active.branchId],
    queryFn: () => getSession(active.branchId),
  });
  const feedback = useTaskExperiment(context.sessionId);
  const current = feedback.data
    ?.filter(
      (row) => row.kind === "outcome" && row.response_id === active.responseId && row.outcome,
    )
    .at(-1);
  const thread = context.threads.find((t) => t.session_id === active.branchId);
  const original = readOriginal(branch.data, thread);
  if (!original || !thread)
    return (
      <p role="status" className="p-3 text-sm text-muted-foreground">
        {branch.isError ? "Could not load feedback discussion" : "Loading feedback discussion…"}
      </p>
    );
  const afterBubble = (bubble: Bubble) => {
    if (bubble.kind !== "assistant" || bubble.lifecycle !== "completed") return null;
    const text = bubble.items
      .filter((item) => item.kind === "text")
      .map((item) => (item.kind === "text" ? item.text : ""))
      .join("\n");
    const proposal = parseFeedbackProposal(text);
    if (!proposal) return null;
    return (
      <SuggestedFeedback
        key={bubble.responseId}
        id={`${active.branchId}:${bubble.responseId}`}
        original={{
          ...original,
          outcome: current?.outcome ?? original.outcome,
          comment: current?.comment ?? original.comment,
          tags: current?.tags ?? original.tags,
        }}
        proposed={proposal}
        onApply={async (details) => {
          const review = context.reviews.current.get(active.responseId);
          if (!review) throw new Error("Reopen the original feedback form to apply changes");
          await review.onReplaceDetails(details);
        }}
      />
    );
  };
  return (
    <FeedbackDisabled>
      <section
        className={
          context.wide
            ? "flex h-full min-h-0 flex-col"
            : "mt-3 flex h-[min(680px,75dvh)] min-h-0 flex-col rounded-xl border bg-background"
        }
        aria-label="Feedback discussion"
      >
        <header className="flex shrink-0 items-start justify-between gap-2 border-b px-3 py-3">
          <div>
            <p className="text-sm font-semibold">Feedback discussion</p>
            <p className="text-xs text-muted-foreground">
              {original.model ?? branch.data?.modelOverride ?? "Responding model"} ·{" "}
              {original.reasoning_effort ?? branch.data?.reasoningEffort ?? "Default"} reasoning
            </p>
          </div>
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            aria-label="Close feedback discussion"
            onClick={() => context.setActive(null)}
          >
            <XIcon className="size-4" />
          </Button>
        </header>
        <div className="min-h-0 flex-1">
          <SideChatPane
            childId={active.branchId}
            inheritedItemIds={thread.inherited_ids}
            persistDraft
            renderAfterBubble={afterBubble}
            transformBubble={(bubble) =>
              bubble.kind === "assistant"
                ? {
                    ...bubble,
                    items: bubble.items.map((item) =>
                      item.kind === "text"
                        ? {
                            ...item,
                            text: item.text.replace(/```feedback-json[\s\S]*?```/g, "").trim(),
                          }
                        : item,
                    ),
                  }
                : bubble.kind === "user"
                  ? {
                      ...bubble,
                      content: bubble.content.map((block) =>
                        block.type === "input_text"
                          ? { ...block, text: block.text.split(CONTEXT_MARKER)[0] }
                          : block,
                      ),
                    }
                  : bubble
            }
            beforeTranscript={
              <div
                className="mb-4 rounded-lg border bg-muted/20 p-3 text-sm"
                aria-label="Original feedback"
              >
                <p className="font-medium">
                  Original feedback · {original.outcome.replaceAll("_", " ")}
                </p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {original.tags.map((tag) => (
                    <span className="rounded border px-2 py-0.5 text-xs" key={tag}>
                      {tag}
                    </span>
                  ))}
                </div>
                {original.comment && <p className="mt-2 whitespace-pre-wrap">{original.comment}</p>}
              </div>
            }
          />
        </div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="shrink-0 border-t"
          onClick={() => context.setActive(null)}
        >
          {context.wide ? "Finish discussion" : "Finish and collapse feedback discussion"}
        </Button>
      </section>
    </FeedbackDisabled>
  );
}
