import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { MoonIcon, RotateCcwIcon, SunIcon } from "lucide-react";
import { useTheme } from "next-themes";
import { Conversation, ConversationContent } from "@/components/ai-elements/conversation";
import {
  BubbleView,
  KeepBottomOnViewportResize,
  ScrollToBottomOnSend,
  WorkingIndicator,
} from "@/components/chat/chatBubbleParts";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import {
  ChatComposer,
  CHAT_COMPOSER_FORM_CLASS,
  COMPOSER_COLUMN_WIDTH,
  ComposerSendButton,
} from "@/components/composer/ChatComposer";
import { ComposerAddMenu } from "@/components/composer/ComposerAddMenu";
import { ComposerContextRing } from "@/components/composer/ComposerContextRing";
import {
  ComposerEffortPicker,
  ComposerHarnessTrigger,
  ComposerHostTrigger,
  ComposerPermissionPicker,
  ComposerWorkspaceBar,
} from "@/components/composer/ComposerControls";
import { ComposerWorkspaceStatus } from "@/components/composer/ComposerWorkspaceStatus";
import {
  FeedbackDiscussionControls,
  FeedbackDiscussionComposerContext,
  type DiscussionVariant,
  type FeedbackDiscussionIntent,
} from "@/components/FeedbackDiscussionControls";
import type { ReviewPerspectiveInput } from "@/components/FeedbackContext";
import { ResponseFeedbackProvider } from "@/components/ResponseFeedbackActions";
import { SuggestedFeedback } from "@/components/SuggestedFeedback";
import { Button } from "@/components/ui/button";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import type { TaskOutcome } from "@/hooks/useTaskExperiment";
import type { Bubble } from "@/lib/renderItems";
import { cn } from "@/lib/utils";
import { CHAT_COLUMN_WIDTH, CHAT_CONVERSATION_CONTENT_CLASS } from "@/pages/chatLayout";
import { ChatHeader } from "@/shell/ChatHeader";
import { ChatStoreSeed, StoryQueryRouter } from "./StoryProviders";
import {
  createFeedbackDiscussionFixture,
  ORIGINAL_RESPONSE_ID,
  SEED_FEEDBACK,
} from "./feedbackDiscussionFixture";

export type { DiscussionVariant } from "@/components/FeedbackDiscussionControls";
export type StartingPoint =
  | "before-feedback"
  | "feedback-saved"
  | "discussion-ready"
  | "inspection-ready"
  | "suggestion-ready";
interface Feedback {
  outcome: TaskOutcome;
  comment: string;
  tags: string[];
}
interface DiscussionTurn {
  id: number;
  request: string;
  intent: FeedbackDiscussionIntent | null;
  snapshot: Feedback | null;
  complete: boolean;
}
interface PrototypeProps {
  variant: DiscussionVariant;
  startingPoint?: StartingPoint;
  responseDelayMs?: number;
  cacheTelemetry?: "reported" | "unreported";
}
const PROPOSED_COMMENT =
  "Local checks pass; live deployment and authenticated behavior still need verification.";
const DISCUSS_PROMPT = "Discuss my feedback on this answer. Do you agree with my assessment?";
const SUGGEST_PROMPT =
  "Suggest clearer wording and tags for my feedback on this answer. Keep my outcome unchanged.";
const INSPECT_PROMPT =
  "Inspect your actions and tool results for this answer. What was verified, and what is still unverified?";
const ANSWER =
  "I’ve added the feedback controls. Local checks pass; deployment has not been verified yet.";
const VARIANT_NAMES = {
  footer: "Footer action",
  "quick-prompts": "Quick prompts",
  inline: "Inline discussion",
};
const KEYBOARD = { submitWithModEnter: false, preventsKeyboardSubmit: false };
const noop = () => undefined;
const MOBILE_MENU = {
  fileViewerOpen: false,
  panelOpen: false,
  terminalFirst: false,
  executionLogsOpen: false,
  filesPanelOpen: false,
  subagentsPanelOpen: false,
  shellsPanelOpen: false,
  hideTerminalsTab: false,
  showShellsTab: false,
  terminalsLength: 0,
  debugMode: false,
  changedCount: 0,
  subagentsWorking: 0,
  agentCount: 1,
  onOpenFiles: noop,
  onOpenChanges: noop,
  onOpenShells: noop,
  onOpenSubagents: noop,
  githubPanelOpen: false,
  onOpenGithub: noop,
  onOpenMainExecutionLog: noop,
};
const STORE_SEED = {
  conversationId: null,
  sessionStatus: "idle" as const,
  status: "idle" as const,
  blocks: [],
  viewers: [],
  flashItemId: null,
  backgroundTaskCount: 0,
  blockedOn: null,
};
function userBubble(id: string, text: string): Bubble {
  return { kind: "user", itemId: id, content: [{ type: "input_text", text }] };
}
function assistantBubble(id: string, text: string): Bubble {
  return {
    kind: "assistant",
    responseId: id,
    stableId: id,
    lifecycle: "completed",
    error: null,
    items: [{ kind: "text", itemId: `${id}-text`, text, final: true }],
    workedForS: 3.2,
  };
}

/** Install the local API adapter before mounting queries; restore it before a replacement mounts. */
function FixtureTransport({
  fetcher,
  children,
}: {
  fetcher: (path: string, init?: RequestInit) => Promise<Response>;
  children: ReactNode;
}) {
  const [ready, setReady] = useState(false);
  useLayoutEffect(() => {
    const previous = globalThis.fetch;
    const adapter: typeof globalThis.fetch = (input, init) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
        window.location.href,
      );
      const apiStart = url.pathname.indexOf("/v1/");
      return apiStart >= 0 ? fetcher(url.pathname.slice(apiStart), init) : previous(input, init);
    };
    globalThis.fetch = adapter;
    setReady(true);
    return () => {
      if (globalThis.fetch === adapter) globalThis.fetch = previous;
    };
  }, [fetcher]);
  return ready ? children : null;
}

/** Production components and auto-save behavior, with only transport and AI turns simulated. */
export function FeedbackDiscussionPrototype(props: PrototypeProps) {
  const [run, setRun] = useState(0);
  const { setTheme } = useTheme();
  return (
    <div className="flex h-dvh min-h-[480px] w-full min-w-0 flex-col bg-background">
      <aside
        aria-label="Prototype inspection controls"
        className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b px-3 py-2 text-xs"
      >
        <div className="min-w-0 flex-1">
          <p className="font-medium">{VARIANT_NAMES[props.variant]}</p>
        </div>
        <Button
          size="icon-xs"
          variant="ghost"
          aria-label="Light theme"
          onClick={() => setTheme("light")}
        >
          <SunIcon className="size-3.5" />
        </Button>
        <Button
          size="icon-xs"
          variant="ghost"
          aria-label="Dark theme"
          onClick={() => setTheme("dark")}
        >
          <MoonIcon className="size-3.5" />
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setRun((current) => current + 1)}>
          <RotateCcwIcon className="size-3.5" />
          Start again
        </Button>
        <p className="w-full text-muted-foreground">
          Preview · Simulated AI, saved data, and cache counters.
        </p>
      </aside>
      <PrototypeConversation key={run} {...props} />
    </div>
  );
}

function PrototypeConversation({
  variant,
  startingPoint = "before-feedback",
  responseDelayMs = 650,
  cacheTelemetry = "unreported",
}: PrototypeProps) {
  const sessionId = `storybook-feedback-${variant}`;
  const [fixture] = useState(() =>
    createFeedbackDiscussionFixture(sessionId, startingPoint !== "before-feedback"),
  );
  const [input, setInput] = useState("");
  const [composerContext, setComposerContext] = useState<FeedbackDiscussionIntent | null>(null);
  const [busy, setBusy] = useState(false);
  const [sendNonce, setSendNonce] = useState(0);
  const [permission, setPermission] = useState("on-request");
  const [effort, setEffort] = useState<string | null>("high");
  const [turns, setTurns] = useState<DiscussionTurn[]>(() =>
    ["discussion-ready", "inspection-ready", "suggestion-ready"].includes(startingPoint)
      ? [
          {
            id: 1,
            request:
              startingPoint === "inspection-ready"
                ? INSPECT_PROMPT
                : startingPoint === "discussion-ready"
                  ? DISCUSS_PROMPT
                  : SUGGEST_PROMPT,
            intent:
              startingPoint === "inspection-ready"
                ? "inspect"
                : startingPoint === "discussion-ready"
                  ? "discuss"
                  : "suggest",
            snapshot:
              startingPoint === "inspection-ready"
                ? null
                : { ...SEED_FEEDBACK, tags: [...SEED_FEEDBACK.tags] },
            complete: true,
          },
        ]
      : [],
  );
  const reviewRef = useRef<ReviewPerspectiveInput | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef(false);
  const nextId = useRef(2);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  useAutoGrowTextarea(inputRef, input, 10);
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );
  function prepare(intent: FeedbackDiscussionIntent) {
    if (busy || (intent !== "inspect" && !reviewRef.current?.saved)) return;
    const text =
      intent === "inspect"
        ? INSPECT_PROMPT
        : intent === "discuss"
          ? DISCUSS_PROMPT
          : SUGGEST_PROMPT;
    setInput((current) => (current.trim() ? `${current}\n\n${text}` : text));
    setComposerContext(intent);
    inputRef.current?.focus();
  }
  function send(text: string, intent = composerContext) {
    const usesFeedback = intent === "discuss" || intent === "suggest";
    if (pending.current || !text.trim() || (usesFeedback && !reviewRef.current?.saved)) return;
    pending.current = true;
    setBusy(true);
    const id = nextId.current++;
    const snapshot = usesFeedback ? fixture.savedFeedback() : null;
    setTurns((current) => [
      ...current,
      { id, request: text.trim(), intent, snapshot, complete: false },
    ]);
    setInput("");
    setComposerContext(intent === "suggest" ? "discuss" : intent);
    setSendNonce((current) => current + 1);
    timer.current = setTimeout(() => {
      setTurns((current) =>
        current.map((turn) => (turn.id === id ? { ...turn, complete: true } : turn)),
      );
      pending.current = false;
      setBusy(false);
      timer.current = null;
    }, responseDelayMs);
  }
  async function apply(details: { comment: string; tags: string[] }) {
    const review = reviewRef.current;
    if (!review?.saved)
      throw new Error("Wait for your current feedback to save before applying changes.");
    await review.onReplaceDetails(details);
  }
  return (
    <FixtureTransport fetcher={fixture.fetcher}>
      <StoryQueryRouter route={`/c/${sessionId}`} seed={fixture.seed}>
        <ChatStoreSeed seed={STORE_SEED}>
          <ResponseFeedbackProvider
            sessionId={sessionId}
            renderPerspective={(responseId, review) => {
              if (responseId !== ORIGINAL_RESPONSE_ID) return null;
              reviewRef.current = review;
              return (
                <FeedbackDiscussionControls
                  variant={variant}
                  busy={busy}
                  saving={!review.saved}
                  onPrepare={prepare}
                  onSubmitInline={(text) =>
                    send(`Discuss my feedback on this answer: ${text}`, "discuss")
                  }
                />
              );
            }}
          >
            <section
              data-chat-surface
              aria-label="Ongoing conversation"
              className="relative flex min-h-0 min-w-0 flex-1 flex-col"
            >
              <ChatHeader
                sidebarOpen={false}
                onOpenSidebar={noop}
                isChildSession={false}
                conversationId={sessionId}
                conversationTitle="Feedback discussion"
                projectName="Omnigent"
                actionConversation={null}
                boundAgent={undefined}
                wrapperLabel={null}
                canShare={false}
                canFork={false}
                onShare={noop}
                onFork={noop}
                hasAgentInfo={false}
                onAgentInfo={noop}
                hasHeaderMenu={false}
                showFilesPanel={false}
                hasRailContent={false}
                rightPanelOpen={false}
                onToggleRightPanel={noop}
                mobileMenu={MOBILE_MENU}
              />
              <div className="@container/chat relative flex min-h-0 flex-1 overflow-hidden">
                <Conversation className="chat-scroll-fade flex-1">
                  <ConversationContent
                    scrollClassName="transcript-hide-native-scrollbar"
                    className={cn(CHAT_CONVERSATION_CONTENT_CLASS, "pt-20", CHAT_COLUMN_WIDTH)}
                  >
                    <ScrollToBottomOnSend nonce={sendNonce} />
                    <KeepBottomOnViewportResize />
                    <BubbleView
                      bubble={userBubble(
                        "storybook-request",
                        "Make feedback discussion available in the ongoing chat, keeping the controls minimal.",
                      )}
                    />
                    <div aria-label="Feedback on the original answer">
                      <BubbleView
                        bubble={assistantBubble(ORIGINAL_RESPONSE_ID, ANSWER)}
                        isLastAssistant={turns.length === 0}
                      />
                      <CacheIndicator reported={cacheTelemetry === "reported"} original />
                    </div>
                    {turns.map((turn) => (
                      <div
                        key={turn.id}
                        className="space-y-4"
                        data-testid="discussion-turn"
                        data-intent={turn.intent ?? "chat"}
                        data-complete={turn.complete}
                      >
                        <BubbleView
                          bubble={userBubble(`storybook-request-${turn.id}`, turn.request)}
                        />
                        {!turn.complete ? (
                          <WorkingIndicator />
                        ) : (
                          <>
                            <BubbleView
                              bubble={assistantBubble(`storybook-reply-${turn.id}`, replyFor(turn))}
                              isLastAssistant={turn.id === turns.at(-1)?.id}
                            />
                            <CacheIndicator reported={cacheTelemetry === "reported"} />
                            {turn.intent === "discuss" && (
                              <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                className="px-0 text-muted-foreground"
                                disabled={busy}
                                onClick={() => prepare("suggest")}
                              >
                                Suggest feedback changes
                              </Button>
                            )}
                            {turn.intent === "suggest" && turn.snapshot && (
                              <SuggestedFeedback
                                original={fixture.savedFeedback() ?? turn.snapshot}
                                proposed={{
                                  comment: PROPOSED_COMMENT,
                                  tags: ["Tests/verification"],
                                }}
                                onApply={apply}
                              />
                            )}
                          </>
                        )}
                      </div>
                    ))}
                  </ConversationContent>
                </Conversation>
              </div>
              <form
                className={CHAT_COMPOSER_FORM_CLASS}
                onSubmit={(event) => {
                  event.preventDefault();
                  send(input);
                }}
              >
                <div className={cn("mx-auto", COMPOSER_COLUMN_WIDTH)}>
                  <ComposerWorkspaceBar>
                    <ComposerWorkspaceStatus
                      workspacePath="/workspace/omnigent"
                      worktreePath={null}
                      isWorktree={false}
                      branch="main"
                      branchState="branch"
                      creationBranch="main"
                      showWorktree={false}
                    />
                    <div className="ml-auto">
                      <ComposerContextRing contextWindow={200000} tokensUsed={80000} />
                    </div>
                  </ComposerWorkspaceBar>
                </div>
                <ChatComposer
                  className={cn("mx-auto", COMPOSER_COLUMN_WIDTH)}
                  keyboard={KEYBOARD}
                  input={{
                    ref: inputRef,
                    "aria-label": "Message the agent",
                    placeholder: "Send a message…",
                    rows: 1,
                    value: input,
                    onChange: (event) => setInput(event.target.value),
                    onKeyDown: (event, intent) => {
                      if (intent.shouldSubmitFromKeyboard) {
                        event.preventDefault();
                        send(input);
                      }
                    },
                  }}
                  slots={{
                    beforeInput: composerContext ? (
                      <FeedbackDiscussionComposerContext
                        intent={composerContext}
                        onDismiss={() => setComposerContext(null)}
                      />
                    ) : undefined,
                  }}
                  actions={{
                    leading: (
                      <>
                        <ComposerAddMenu
                          disabled={false}
                          onAttach={noop}
                          attachDisabled
                          goalDisabled
                          planDisabled
                          planActive={false}
                        />
                        <span className="inline-flex min-w-0 items-center gap-1.5 text-sm font-medium">
                          <ComposerAgentIcon
                            agent={{ name: "codex-native-ui", harness: "codex-native" }}
                          />
                          <span className="hidden sm:inline">Codex</span>
                        </span>
                        <ComposerHostTrigger label="Preview host" status="online" />
                        <ComposerPermissionPicker
                          label="Permission mode"
                          value={permission === "on-request" ? "Ask" : "Auto"}
                          harness="codex-native"
                          selectedValue={permission}
                          options={[
                            { value: "on-request", label: "Ask" },
                            { value: "never", label: "Auto" },
                          ]}
                          onSelect={setPermission}
                        />
                      </>
                    ),
                    trailing: (
                      <>
                        <div className="relative flex w-20 min-[480px]:w-32 shrink-0 min-w-0 flex-col items-end justify-center rounded-lg [&>span]:w-full [&_button]:w-full">
                          <ComposerHarnessTrigger label="Model: gpt-5.4" model="gpt-5.4" />
                        </div>
                        <ComposerEffortPicker
                          value={effort}
                          options={[
                            { value: "low", label: "Low" },
                            { value: "medium", label: "Medium" },
                            { value: "high", label: "High" },
                          ]}
                          onSelect={setEffort}
                        />
                        <ComposerMicButton
                          reserveSpace
                          className="size-8 md:size-7"
                          disabled
                          onTranscript={noop}
                        />
                        <ComposerSendButton
                          label="Send"
                          disabled={busy || !input.trim()}
                          onClick={() => send(input)}
                        />
                      </>
                    ),
                  }}
                />
              </form>
            </section>
          </ResponseFeedbackProvider>
        </ChatStoreSeed>
      </StoryQueryRouter>
    </FixtureTransport>
  );
}

function replyFor(turn: DiscussionTurn): string {
  if (turn.intent === "inspect") {
    return "The action record supports **local validation**:\n\n- Changed the feedback controls and added coverage for saved feedback.\n- Ran the focused UI tests successfully.\n- Built the local web bundle successfully.\n\nThere is **no live deployment or authenticated session check** in the record. The answer should be clearer about the scope of its verification.";
  }
  if (turn.intent === "suggest" && turn.snapshot) {
    return `Your **${turn.snapshot.outcome.replaceAll("_", " ")}** outcome remains unchanged. Here is a more specific comment and tag for your review. You can edit the suggestion before accepting it.`;
  }
  if (turn.intent === "discuss" && turn.snapshot) {
    return `Your **${turn.snapshot.outcome.replaceAll("_", " ")}** assessment makes sense: your comment asks for live verification before you consider the task complete. Does that outcome refer to the UI being unfinished, or to verification still being missing? Clarifying that would make your feedback easier to understand.`;
  }
  return "We can continue with the remaining live verification next.";
}

function CacheIndicator({ reported, original = false }: { reported: boolean; original?: boolean }) {
  return (
    <p className="mt-1 text-xs text-muted-foreground">
      {reported
        ? original
          ? "Cache reuse: 90% · 72,000 / 80,000 input tokens"
          : "Cache reuse: 88% · 88,000 / 100,000 input tokens"
        : "Cache reuse: Unavailable"}
    </p>
  );
}
