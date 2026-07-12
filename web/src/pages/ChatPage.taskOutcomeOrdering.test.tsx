// Anchoring tests for the Task outcome brief card and the bubbles it
// sits next to. The card MUST live directly after the matching
// completed assistant bubble in DOM order; it must never appear
// between the user message and the assistant response; it must never
// duplicate; and it must never render during streaming or before its
// outcome payload is ready.
//
// These tests render real components in a React Testing Library DOM
// (no shallow render): DOM-order assertions verify the actual visible
// position, not just a render tree.
//
// KEY INVARIANT: TaskOutcomeBriefCard must only be mounted when an
// authoritative mapping exists from bubble.stableId → taskRunResponseId.
// NO FALLBACK to bubble.responseId.

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useParams } from "@/lib/routing";
import type { Bubble } from "@/lib/renderItems";
import type { TaskRunDetailResponse } from "@/lib/taskOutcomes";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { BubbleView } from "./ChatPage";

const FILE_VIEWER_NOOP = {
  openFile: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: null,
  workspaceHome: null,
};

vi.mock("@/lib/routing", async () => {
  const actual = await vi.importActual<typeof import("@/lib/routing")>("@/lib/routing");
  return {
    ...actual,
    useParams: vi.fn(),
    useNavigate: vi.fn(() => () => {}),
    useLocation: vi.fn(),
  };
});

// vi.mock factories run before module evaluation, so the mutable
// fixture store must be hoisted via `vi.hoisted`. The factory uses
// the hoisted store + a `readyFor` helper that reads it on every call
// so test-side mutations of the fixture are visible in the rendered component.
const mockReady = vi.hoisted(() => {
  const fixtures: Record<string, TaskRunDetailResponse> = {};
  return {
    fixtures,
    setFixture(key: string, value: TaskRunDetailResponse | null) {
      if (value === null) delete fixtures[key];
      else fixtures[key] = value;
    },
    readyFor(sessionId: string, responseId: string) {
      const detail = fixtures[`${sessionId}:${responseId}`] ?? null;
      if (detail) {
        return {
          phase: "ready" as const,
          detail,
          error: null,
          identityMismatch: false,
          retry: () => {},
        };
      }
      return {
        phase: "loading" as const,
        detail: null,
        error: null,
        identityMismatch: false,
        retry: () => {},
      };
    },
  };
});

vi.mock("@/lib/useTaskRunReadiness", () => ({
  useTaskRunForResponse: (sessionId: string, responseId: string) =>
    mockReady.readyFor(sessionId, responseId),
  TASK_RUN_READINESS_DELAYS_MS: [],
  TASK_RUN_READINESS_MAX_ATTEMPTS: 1,
  TaskRunFetchError: class TaskRunFetchError extends Error {
    status: number;
    constructor(status: number, message?: string) {
      super(message ?? "");
      this.status = status;
    }
  },
  TaskRunResponseIdentityError: class TaskRunResponseIdentityError extends Error {},
}));

interface ConversationPieceProps {
  bubbles: Bubble[];
  /** Map from bubble stableId to task_run.response_id. NO FALLBACK. */
  bubbleToTaskRunResponseId?: ReadonlyMap<string, string> | null;
}

function ConversationPiece({ bubbles, bubbleToTaskRunResponseId = null }: ConversationPieceProps) {
  return (
    <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
      <div data-testid="conversation-flow">
        {bubbles.map((bubble, index) => {
          const taskRunResponseId =
            bubble.kind === "assistant" && bubble.lifecycle === "completed"
              ? bubbleToTaskRunResponseId?.get(bubble.stableId)
              : undefined;

          // Only render outcome if this is the LAST bubble with this stableId
          const isCanonicalOwner =
            bubble.kind === "assistant" && taskRunResponseId !== undefined
              ? !bubbles.some(
                  (candidate, j) =>
                    j > index &&
                    candidate.kind === "assistant" &&
                    candidate.stableId === bubble.stableId,
                )
              : false;

          const key =
            bubble.kind === "assistant"
              ? `assistant:${bubble.stableId}:${index}`
              : bubble.kind === "user"
                ? `user:${bubble.itemId}`
                : `${bubble.kind}:${index}`;

          return (
            <BubbleView
              key={key}
              bubble={bubble}
              renderOutcome={isCanonicalOwner}
              taskRunResponseId={isCanonicalOwner ? taskRunResponseId : undefined}
            />
          );
        })}
      </div>
    </FileViewerContext.Provider>
  );
}

function userBubble(text = "Hi"): Extract<Bubble, { kind: "user" }> {
  return {
    kind: "user",
    itemId: "u1",
    stableKey: undefined,
    content: [{ type: "input_text", text }],
  };
}

function assistantBubble(
  responseId: string,
  lifecycle: Extract<Bubble, { kind: "assistant" }>["lifecycle"],
  text = "Hi! How can I help?",
): Extract<Bubble, { kind: "assistant" }> {
  return {
    kind: "assistant",
    responseId,
    stableId: `${responseId}-stable`,
    lifecycle,
    error: null,
    items: [{ kind: "text", itemId: `${responseId}_msg`, text, final: true }],
  };
}

function reasoningBubble(responseId: string): Extract<Bubble, { kind: "assistant" }> {
  return {
    kind: "assistant",
    responseId,
    stableId: `${responseId}-stable`,
    lifecycle: "completed",
    error: null,
    items: [
      {
        kind: "reasoning",
        itemId: `${responseId}_reasoning`,
        text: "Thinking...",
        duration: 0.5,
      },
    ],
  };
}

function mixedBubble(
  responseId: string,
  finalText: string,
): Extract<Bubble, { kind: "assistant" }> {
  return {
    kind: "assistant",
    responseId,
    stableId: `${responseId}-stable`,
    lifecycle: "completed",
    error: null,
    items: [
      {
        kind: "reasoning",
        itemId: `${responseId}_reasoning`,
        text: "Thinking...",
        duration: 0.5,
      },
      { kind: "text", itemId: `${responseId}_msg`, text: finalText, final: true },
    ],
  };
}

const READY_DETAIL: TaskRunDetailResponse = {
  run: {
    id: "run-1",
    conversation_id: "conv-x",
    response_id: "resp_1",
    triggering_message_id: null,
    project_path: null,
    task_description: null,
    proposed_task_family: "feature_implementation",
    estimated_difficulty: null,
    harness_id: "h1",
    requested_route_id: "auto/coding",
    selected_provider: "anthropic",
    selected_model: "claude-4.7",
    reasoning_effort: "medium",
    permission_mode: null,
    omniroute_decision_id: null,
    selection_strategy: null,
    billing_class: "pro",
    fallback_used: false,
    terminal_status: "completed",
    started_at: null,
    terminal_at: null,
    duration_ms: null,
    input_tokens: null,
    output_tokens: null,
    total_cost_usd: null,
    response_summary: null,
    changed_files: null,
    commit_sha: null,
    failure_error_code: null,
    failure_error_message: null,
    langfuse_trace_id: null,
    langfuse_observation_id: null,
    created_at: 0,
    updated_at: 0,
  },
  evaluation: {
    id: "eval-1",
    task_run_id: "run-1",
    evaluator_type: "llm",
    evaluator_provider: null,
    evaluator_model: null,
    evaluator_route_id: null,
    verdict: "success",
    confidence: 0.9,
    quality_score: 5,
    proposed_task_family: "feature_implementation",
    reasoning: null,
    evidence: null,
    unresolved_issues: null,
    created_at: 0,
  },
  review: null,
  any_review: null,
  langfuse_pending: false,
};

beforeEach(async () => {
  for (const k of Object.keys(mockReady.fixtures)) delete mockReady.fixtures[k];
  vi.mocked(useParams).mockReturnValue({ conversationId: "conv-x" });
});

function setFixture(key: string, value: TaskRunDetailResponse | null) {
  mockReady.setFixture(key, value);
}

afterEach(cleanup);

describe("Task outcome card anchoring — DOM order", () => {
  it("renders card after assistant bubble, not between user and assistant", async () => {
    setFixture("conv-x:resp_1", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Hi! How can I help you today?"),
    ];
    const anchors = new Map<string, string>([["resp_1-stable", "resp_1"]]);
    const { container } = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );
    const card = await screen.findByTestId("task-outcome-brief-card");
    expect(card).toBeInTheDocument();

    const slot = screen.getByTestId("assistant-outcome-slot");
    expect(slot.getAttribute("data-response-id")).toBe("resp_1");
    expect(within(slot).getByTestId("task-outcome-brief-card")).toBe(card);

    const userMessage = container.querySelector(
      '[data-role="user"][data-testid="message-bubble"]',
    )!;
    const assistantMessage = container.querySelector(
      '[data-role="assistant"][data-testid="message-bubble"]',
    )!;
    const outcomeSlot = container.querySelector('[data-testid="assistant-outcome-slot"]')!;

    expect(
      userMessage.compareDocumentPosition(assistantMessage) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      assistantMessage.compareDocumentPosition(outcomeSlot) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      userMessage.compareDocumentPosition(outcomeSlot) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(userMessage.contains(card)).toBe(false);
    expect(assistantMessage.contains(card)).toBe(false);
  });

  it("does not render outcome card during streaming", () => {
    const bubbles: Bubble[] = [userBubble("Hi"), assistantBubble("resp_1", "streaming")];
    const anchors = new Map<string, string>([["resp_1-stable", "resp_1"]]);
    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);
    expect(screen.queryByTestId("assistant-outcome-slot")).toBeNull();
    expect(screen.queryByTestId("task-outcome-brief-card")).toBeNull();
  });

  it("preserves per-response association for multiple turns", async () => {
    const detailA = {
      ...READY_DETAIL,
      run: { ...READY_DETAIL.run, id: "runA", response_id: "resp_1" },
    };
    const detailB = {
      ...READY_DETAIL,
      run: { ...READY_DETAIL.run, id: "runB", response_id: "resp_2" },
    };
    setFixture("conv-x:resp_1", detailA);
    setFixture("conv-x:resp_2", detailB);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Hi! How can I help?"),
      userBubble("Write a function"),
      assistantBubble("resp_2", "completed", "Sure, here it is."),
    ];
    const anchors = new Map<string, string>([
      ["resp_1-stable", "resp_1"],
      ["resp_2-stable", "resp_2"],
    ]);
    const { container } = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );

    await waitFor(() => expect(screen.getAllByTestId("assistant-outcome-slot")).toHaveLength(2));
    const cards = screen.getAllByTestId("task-outcome-brief-card");
    expect(cards).toHaveLength(2);

    const slots = screen.getAllByTestId("assistant-outcome-slot");
    expect(slots.map((s) => s.getAttribute("data-response-id"))).toEqual(["resp_1", "resp_2"]);
    expect(cards[0].getAttribute("data-task-run-id")).toBe("runA");
    expect(cards[1].getAttribute("data-task-run-id")).toBe("runB");

    const flow = container.querySelector('[data-testid="conversation-flow"]')!;
    const roleChain = Array.from(flow.children).map(
      (el) =>
        `${el.getAttribute("data-role") ?? "-"}/${el.getAttribute("data-response-id") ?? "-"}`,
    );
    expect(roleChain).toEqual([
      "user/-",
      "assistant/resp_1",
      "-/resp_1",
      "user/-",
      "assistant/resp_2",
      "-/resp_2",
    ]);
  });

  it("does not place outcome card before assistant response completes", async () => {
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Hi! How can I help?"),
    ];
    const anchors = new Map<string, string>([["resp_1-stable", "resp_1"]]);

    const initial = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );
    expect(initial.queryByTestId("task-outcome-brief-card")).toBeNull();
    initial.unmount();

    setFixture("conv-x:resp_1", READY_DETAIL);
    const view = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );
    await waitFor(() => expect(screen.queryByTestId("task-outcome-brief-card")).not.toBeNull());

    const flow = view.container.querySelector('[data-testid="conversation-flow"]')!;
    const userMsg = flow.children[0] as HTMLElement;
    const assistantMsg = flow.children[1] as HTMLElement;
    const slot = flow.querySelector('[data-testid="assistant-outcome-slot"]') as HTMLElement;

    expect(
      (assistantMsg.compareDocumentPosition(slot) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      (userMsg.compareDocumentPosition(slot) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    view.unmount();
  });

  it("does not duplicate card on remount", async () => {
    setFixture("conv-x:resp_1", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Hi! How can I help?"),
    ];
    const anchors = new Map<string, string>([["resp_1-stable", "resp_1"]]);

    const first = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );
    await first.findAllByTestId("task-outcome-brief-card");
    first.unmount();

    const second = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );
    await waitFor(() => {
      expect(second.getAllByTestId("task-outcome-brief-card")).toHaveLength(1);
    });
    second.unmount();
  });
});

describe("Reasoning bubble should NOT get outcome - the core fix", () => {
  it("no outcome slot follows reasoning-only bubble", async () => {
    setFixture("conv-x:resp_omni", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      reasoningBubble("msg_reasoning"),
      assistantBubble("msg_final", "completed", "Hi! How can I help you?"),
    ];
    const anchors = new Map<string, string>([["msg_final-stable", "resp_omni"]]);

    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);

    await screen.findByTestId("task-outcome-brief-card");

    const slots = screen.getAllByTestId("assistant-outcome-slot");
    expect(slots).toHaveLength(1);
    expect(slots[0].getAttribute("data-response-id")).toBe("msg_final");
    expect(
      screen.queryByTestId("assistant-outcome-slot[data-response-id='msg_reasoning']"),
    ).toBeNull();
  });

  it("reasoning bubble with matching responseId does NOT get outcome", async () => {
    setFixture("conv-x:msg_reasoning", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      reasoningBubble("msg_reasoning"),
      assistantBubble("msg_final", "completed", "Hi! How can I help you?"),
    ];
    const anchors = new Map<string, string>([["msg_final-stable", "msg_reasoning"]]);

    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);

    await screen.findByTestId("task-outcome-brief-card");

    const slots = screen.getAllByTestId("assistant-outcome-slot");
    expect(slots).toHaveLength(1);
    expect(slots[0].getAttribute("data-response-id")).toBe("msg_final");
    expect(
      screen.queryByTestId("assistant-outcome-slot[data-response-id='msg_reasoning']"),
    ).toBeNull();
  });

  it("DOM order: reasoning → final answer → outcome slot", async () => {
    setFixture("conv-x:resp_omni", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      reasoningBubble("msg_reasoning"),
      mixedBubble("msg_final", "Final answer here"),
    ];
    const anchors = new Map<string, string>([["msg_final-stable", "resp_omni"]]);
    const { container } = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );

    await screen.findByTestId("task-outcome-brief-card");

    const flow = container.querySelector('[data-testid="conversation-flow"]')!;
    const children = Array.from(flow.children);

    const reasoningIdx = children.findIndex(
      (el) => el.getAttribute("data-response-id") === "msg_reasoning",
    );
    const finalIdx = children.findIndex(
      (el) => el.getAttribute("data-response-id") === "msg_final",
    );
    const outcomeIdx = children.findIndex(
      (el) => el.getAttribute("data-testid") === "assistant-outcome-slot",
    );

    expect(reasoningIdx).toBeGreaterThan(-1);
    expect(finalIdx).toBeGreaterThan(reasoningIdx);
    expect(outcomeIdx).toBeGreaterThan(finalIdx);
    expect(outcomeIdx).not.toBe(reasoningIdx + 1);
  });
});

describe("OpenCode-native response ID association", () => {
  it("attaches outcome card to OpenCode assistant bubble", async () => {
    setFixture("conv-x:resp_omni_1", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("msg_opencode_assistant_1", "completed", "Processing..."),
    ];
    const anchors = new Map<string, string>([["msg_opencode_assistant_1-stable", "resp_omni_1"]]);
    const { container } = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );

    const card = await screen.findByTestId("task-outcome-brief-card");
    const slot = screen.getByTestId("assistant-outcome-slot");
    expect(slot.getAttribute("data-response-id")).toBe("msg_opencode_assistant_1");
    expect(slot.getAttribute("data-task-run-response-id")).toBe("resp_omni_1");
    expect(card.getAttribute("data-task-run-id")).toBe("run-1");

    const flow = container.querySelector('[data-testid="conversation-flow"]')!;
    const userMsg = flow.children[0] as HTMLElement;
    const assistantMsg = flow.children[1] as HTMLElement;
    const outcomeSlot = flow.children[2] as HTMLElement;

    expect(
      (userMsg.compareDocumentPosition(assistantMsg) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      (assistantMsg.compareDocumentPosition(outcomeSlot) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("handles multiple OpenCode-native turns", async () => {
    const detail1 = {
      ...READY_DETAIL,
      run: { ...READY_DETAIL.run, id: "run-1", response_id: "resp_omni_1" },
    };
    const detail2 = {
      ...READY_DETAIL,
      run: { ...READY_DETAIL.run, id: "run-2", response_id: "resp_omni_2" },
    };
    setFixture("conv-x:resp_omni_1", detail1);
    setFixture("conv-x:resp_omni_2", detail2);

    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("msg_opencode_1", "completed", "First response"),
      userBubble("Second request"),
      assistantBubble("msg_opencode_2", "completed", "Second response"),
    ];
    const anchors = new Map<string, string>([
      ["msg_opencode_1-stable", "resp_omni_1"],
      ["msg_opencode_2-stable", "resp_omni_2"],
    ]);

    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);

    await waitFor(() => {
      expect(screen.getAllByTestId("assistant-outcome-slot")).toHaveLength(2);
    });

    const slots = screen.getAllByTestId("assistant-outcome-slot");
    expect(slots[0].getAttribute("data-task-run-response-id")).toBe("resp_omni_1");
    expect(slots[1].getAttribute("data-task-run-response-id")).toBe("resp_omni_2");
  });

  it("fail closed when there's no mapping", async () => {
    setFixture("conv-x:resp_omni_1", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Real user message"),
      assistantBubble("msg_opencode_1", "completed", "Response"),
    ];
    const anchors = new Map<string, string>();

    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);

    expect(screen.queryByTestId("task-outcome-brief-card")).toBeNull();
    expect(screen.queryByTestId("assistant-outcome-slot")).toBeNull();
  });
});

describe("No mapping means no card - the critical invariant", () => {
  it("empty map produces zero outcome slots", async () => {
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "First response"),
      assistantBubble("resp_2", "completed", "Second response"),
      assistantBubble("resp_3", "completed", "Third response"),
    ];
    const anchors = new Map<string, string>();

    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);

    expect(screen.queryByTestId("assistant-outcome-slot")).toBeNull();
    expect(screen.queryByTestId("task-outcome-brief-card")).toBeNull();
  });

  it("partial mapping - only mapped bubbles get slots", async () => {
    setFixture("conv-x:resp_2", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "First response"),
      assistantBubble("resp_2", "completed", "Second response"),
      assistantBubble("resp_3", "completed", "Third response"),
    ];
    const anchors = new Map<string, string>([["resp_2-stable", "resp_2"]]);

    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);

    await waitFor(() => {
      expect(screen.queryByTestId("task-outcome-brief-card")).not.toBeNull();
    });

    const slots = screen.getAllByTestId("assistant-outcome-slot");
    expect(slots).toHaveLength(1);
    expect(slots[0].getAttribute("data-response-id")).toBe("resp_2");
  });

  it("no fallback to bubble.responseId when mapping is missing", async () => {
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Response"),
    ];
    const anchors = new Map<string, string>([["other-stable", "other"]]);

    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);

    expect(screen.queryByTestId("assistant-outcome-slot")).toBeNull();
    expect(screen.queryByTestId("task-outcome-brief-card")).toBeNull();
  });
});

describe("Reconnect duplicates", () => {
  it("one canonical bubble owns the outcome - slot count is exactly 1", async () => {
    setFixture("conv-x:resp_1", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Response"),
      assistantBubble("resp_1", "completed", "Response"),
    ];
    const anchors = new Map<string, string>([["resp_1-stable", "resp_1"]]);

    render(<ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />);

    await waitFor(() => {
      expect(screen.getAllByTestId("task-outcome-brief-card")).toHaveLength(1);
    });

    const slots = screen.getAllByTestId("assistant-outcome-slot");
    expect(slots).toHaveLength(1);
  });
});

describe("Loading becomes ready in one slot", () => {
  it("slot count stays exactly 1", async () => {
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Response"),
    ];
    const anchors = new Map<string, string>([["resp_1-stable", "resp_1"]]);

    const first = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );
    expect(screen.queryAllByTestId("assistant-outcome-slot")).toHaveLength(1);
    expect(screen.queryByTestId("task-outcome-brief-card")).toBeNull();
    first.unmount();

    setFixture("conv-x:resp_1", READY_DETAIL);

    const { unmount } = render(
      <ConversationPiece bubbles={bubbles} bubbleToTaskRunResponseId={anchors} />,
    );

    await waitFor(() => {
      expect(screen.queryByTestId("task-outcome-brief-card")).not.toBeNull();
    });

    expect(screen.getAllByTestId("assistant-outcome-slot")).toHaveLength(1);
    unmount();
  });
});
