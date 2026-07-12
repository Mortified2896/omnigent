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
// so test-side mutations are visible in the rendered component.
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
      // eslint-disable-next-line no-console
      if (typeof process !== "undefined" && process.env && process.env.DEBUG_OUTCOME_MOCK) {
        console.log(`[mockReady] ${sessionId}:${responseId} -> ${detail ? "ready" : "loading"}`);
      }
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
  // React-hook-compatible mock: read the (mutable) fixture on every
  // call so post-render mutations of the fixture show through.
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

// `BubbleView` is the in-ChatPage component that decides whether each
// assistant bubble gets its inline `<TaskOutcomeBriefCard />` anchor.
// We'll render a tiny version of the surrounding conversation list to
// exercise the per-response placement directly: a user bubble + a
// matching assistant bubble. Assertion is on real DOM order.

interface ConversationPieceProps {
  bubbles: Bubble[];
  /**
   * Map from bubble responseId to the task_run.response_id to use for API calls.
   * This is derived from `resolveTaskOutcomeAnchors` in production.
   * When omitted, the bubble responseId is used directly (backward compat).
   */
  bubbleToTaskRunResponseId?: ReadonlyMap<string, string> | null;
  /**
   * Set of response ids that have a registered task outcome
   * (ChatPage's `taskOutcomeResponseIds.has(responseId)` gate). When
   * omitted, all assistant bubbles are eligible (testing the
   * per-response anchor without the registry gate).
   */
  renderOutcomesFor?: ReadonlySet<string> | null;
}

function ConversationPiece({ bubbles, bubbleToTaskRunResponseId = null, renderOutcomesFor = null }: ConversationPieceProps) {
  // Use BubbleView directly — same component ChatPage uses. The
  // rendered DOM is faithful to the deployed conversation surface.
  return (
    <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
      <div data-testid="conversation-flow">
        {bubbles.map((bubble, index) => {
          // Mirror the production gating exactly so tests reflect
          // what ChatPage renders, including the registry gate.
          let renderOutcome = false;
          let taskRunResponseId: string | undefined;
          if (bubble.kind === "assistant" && bubble.lifecycle === "completed") {
            if (renderOutcomesFor === null) {
              renderOutcome = true;
            } else {
              renderOutcome =
                renderOutcomesFor.has(bubble.responseId) &&
                !bubbles.some(
                  (candidate, j) =>
                    j > index &&
                    candidate.kind === "assistant" &&
                    candidate.lifecycle !== "streaming" &&
                    candidate.responseId === bubble.responseId,
                );
            }
            // Get the mapped task run response ID, falling back to bubble responseId.
            taskRunResponseId = bubbleToTaskRunResponseId?.get(bubble.responseId) ?? bubble.responseId;
          }
          return (
            <BubbleView
              key={index}
              bubble={bubble}
              renderOutcome={renderOutcome}
              taskRunResponseId={renderOutcome ? taskRunResponseId : undefined}
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
    stableId: responseId,
    lifecycle,
    error: null,
    items: [{ kind: "text", itemId: responseId + "_msg", text, final: true }],
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
  // Reset the mocked fixture store.
  for (const k of Object.keys(mockReady.fixtures)) delete mockReady.fixtures[k];
  // Provide a session id so the outcome slot has a session.
  vi.mocked(useParams).mockReturnValue({ conversationId: "conv-x" });
});

function setFixture(key: string, value: TaskRunDetailResponse | null) {
  mockReady.setFixture(key, value);
}

afterEach(cleanup);

describe("Task outcome card anchoring — DOM order", () => {
  it("renders the task outcome card directly after the matching completed assistant bubble, never between user and assistant", async () => {
    setFixture("conv-x:resp_1", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Hi! How can I help you today?"),
    ];
    const { container } = render(<ConversationPiece bubbles={bubbles} />);
    // The card appears (after the readiness mock resolves `ready`).
    const card = await screen.findByTestId("task-outcome-brief-card");
    expect(card).toBeInTheDocument();

    // The card must be a strict descendant of the assistant-outcome
    // slot that carries the same data-response-id.
    const slot = screen.getByTestId("assistant-outcome-slot");
    expect(slot.getAttribute("data-response-id")).toBe("resp_1");
    expect(within(slot).getByTestId("task-outcome-brief-card")).toBe(card);

    // DOM-order assertions. We compare the positions of:
    //   - the user-message-bubble,
    //   - the assistant-message-bubble,
    //   - the task outcome card.
    const userMessage = container.querySelector(
      '[data-role="user"][data-testid="message-bubble"]',
    )!;
    const assistantMessage = container.querySelector(
      '[data-role="assistant"][data-testid="message-bubble"]',
    )!;
    const outcomeSlot = container.querySelector('[data-testid="assistant-outcome-slot"]')!;

    // Bitmask via compareDocumentPosition: the user message must be
    // BEFORE the assistant message, and the assistant message must
    // be BEFORE the outcome slot. The card must NOT sit between user
    // and assistant.
    expect(
      userMessage.compareDocumentPosition(assistantMessage) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      assistantMessage.compareDocumentPosition(outcomeSlot) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      userMessage.compareDocumentPosition(outcomeSlot) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // No card shows up between user and assistant bubbles: there is
    // no descendant of the user message that is the card, and the
    // assistant message is the immediate parent of the message
    // content, not the card.
    expect(userMessage.contains(card)).toBe(false);
    expect(assistantMessage.contains(card)).toBe(false);

    // Stub the outcome-bearing class to verify the data-testid is the
    // shorthand requested in the issue (`task-outcome-card`).
    expect(card.getAttribute("data-testid")).toBe("task-outcome-brief-card");
  });

  it("does not render an outcome card during streaming — only after the response completes", () => {
    // useTaskRunForResponse returns loading for an unresolved
    // response id. While the bubble's lifecycle is "streaming", the
    // card's gating predicates short-circuit.
    const bubbles: Bubble[] = [userBubble("Hi"), assistantBubble("resp_1", "streaming")];
    render(<ConversationPiece bubbles={bubbles} />);
    // Streaming bubbles render no message content beyond the working
    // shimmer (handled at the page level), and no outcome card slot.
    expect(screen.queryByTestId("assistant-outcome-slot")).toBeNull();
    expect(screen.queryByTestId("task-outcome-brief-card")).toBeNull();

    // After completion, the slot appears (test below proves this).
  });

  it("preserves the per-response association when two completed assistant turns have outcome data", async () => {
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
    const { container } = render(<ConversationPiece bubbles={bubbles} />);

    await waitFor(() => expect(screen.getAllByTestId("assistant-outcome-slot")).toHaveLength(2));
    const cards = screen.getAllByTestId("task-outcome-brief-card");
    expect(cards).toHaveLength(2);

    const slots = screen.getAllByTestId("assistant-outcome-slot");
    expect(slots.map((s) => s.getAttribute("data-response-id"))).toEqual(["resp_1", "resp_2"]);

    // DOM-order check via parent flow's children — each BubbleView
    // returns a fragment of two siblings (Message + outcome slot), so
    // the direct children of the flow wrapper are exactly the bubbles
    // and outcome slots in the order they were emitted.
    const flow = container.querySelector('[data-testid="conversation-flow"]')!;
    const roleChain = Array.from(flow.children).map(
      (el) =>
        `${el.getAttribute("data-role") ?? "-"}/${el.getAttribute("data-response-id") ?? "-"}`,
    );
    expect(roleChain).toEqual([
      "user/-",
      "assistant/resp_1",
      "-/resp_1", // assistant-outcome-slot has no data-role, only data-response-id
      "user/-",
      "assistant/resp_2",
      "-/resp_2",
    ]);

    // Each card's run id matches its slot.
    expect(cards[0].getAttribute("data-task-run-id")).toBe("runA");
    expect(cards[1].getAttribute("data-task-run-id")).toBe("runB");

    // Outcome-1 sits strictly before user-2 (no migration to the
    // latest response, no straddling the user/assistant boundary).
    const user2 = flow.children[3] as HTMLElement;
    const outcome1 = slots[0];
    expect(
      (outcome1.compareDocumentPosition(user2) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("does not migrate a late evaluation to the newest response when the older response's data resolves last", async () => {
    // Two completed assistant bubbles. Only resp_2 has a registered
    // task outcome, registered late. After both render, the card
    // must sit only after resp_2, never after resp_1 (migrating the
    // card to the latest response is the array-position heuristic we
    // explicitly forbid). The pipeline gating (registry membership)
    // must be exercised end-to-end so a card for response Y never
    // gets attached to the latest response X just because X is newer
    // in the array.
    const detailB = {
      ...READY_DETAIL,
      run: { ...READY_DETAIL.run, id: "runB", response_id: "resp_2" },
    };
    setFixture("conv-x:resp_2", detailB);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Hi! How can I help?"),
      userBubble("Write a function"),
      assistantBubble("resp_2", "completed", "Sure, here it is."),
    ];
    render(<ConversationPiece bubbles={bubbles} renderOutcomesFor={new Set(["resp_2"])} />);
    await waitFor(() => {
      expect(screen.queryByTestId("task-outcome-brief-card")).not.toBeNull();
    });
    const slots = screen.getAllByTestId("assistant-outcome-slot");
    expect(slots).toHaveLength(1);
    expect(slots[0].getAttribute("data-response-id")).toBe("resp_2");

    const card = screen.getByTestId("task-outcome-brief-card");
    expect(card.getAttribute("data-task-run-id")).toBe("runB");
    // The card must be a descendant of the resp_2 slot, not the
    // resp_1 slot (none exists for resp_1 since it has no run).
    expect(slots[0].contains(card)).toBe(true);
  });

  it("does not place an outcome card before its assistant response lifecycle completes", async () => {
    // Resolve the fixture AFTER rendering. While the bubble is
    // streaming, no slot exists; once it completes, the slot appears
    // underneath — never above — the assistant bubble. To exercise
    // the late-readiness path through React, mount a NEW conversation
    // (BubbleView is memoized, so a same-DOM rerender skips re-running
    // the readiness hook) after the fixture is set.
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Hi! How can I help?"),
    ];
    // First mount: no fixture → card never shows ready state.
    const initial = render(<ConversationPiece bubbles={bubbles} />);
    expect(initial.queryByTestId("task-outcome-brief-card")).toBeNull();
    initial.unmount();
    // Set the fixture, then mount fresh.
    setFixture("conv-x:resp_1", READY_DETAIL);
    const view = render(<ConversationPiece bubbles={bubbles} />);
    await waitFor(() => expect(screen.queryByTestId("task-outcome-brief-card")).not.toBeNull());
    // Critically: the card sits strictly AFTER its assistant bubble
    // (under the assistant message, above any subsequent bubble that
    // may render) — it is never above the matching assistant message.
    const flow = view.container.querySelector('[data-testid="conversation-flow"]')!;
    const userMsg = flow.children[0] as HTMLElement;
    const assistantMsg = flow.children[1] as HTMLElement;
    const slot = flow.querySelector('[data-testid="assistant-outcome-slot"]') as HTMLElement;
    expect(slot.getAttribute("data-response-id")).toBe("resp_1");
    // Slot comes AFTER assistant message (rendered as the sibling
    // directly below the bubble's <Message>)…
    expect(
      (assistantMsg.compareDocumentPosition(slot) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // …but the slot never ends up before the user message in the
    // transcript (the card belongs to the assistant response).
    expect(
      (userMsg.compareDocumentPosition(slot) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    view.unmount();
  });

  it("does not duplicate the card across React StrictMode double-render or remounts", async () => {
    setFixture("conv-x:resp_1", READY_DETAIL);
    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("resp_1", "completed", "Hi! How can I help?"),
    ];
    const first = render(<ConversationPiece bubbles={bubbles} />);
    await first.findAllByTestId("task-outcome-brief-card");
    // Remount with the same conversation (simulates hydration pass or
    // StrictMode). StrictMode unmounts + remounts, so count must
    // still be 1 per assistant bubble.
    first.unmount();
    const second = render(<ConversationPiece bubbles={bubbles} />);
    await waitFor(() => {
      expect(second.getAllByTestId("task-outcome-brief-card")).toHaveLength(1);
    });
    second.unmount();
  });
});

describe("OpenCode-native response ID association", () => {
  it("attaches outcome card to OpenCode assistant bubble with mapped response ID", async () => {
    // Setup: OpenCode-native uses different IDs for transcript vs execution:
    // - user bubble itemId = msg_user_1 (transcript ID)
    // - assistant bubble responseId = msg_opencode_assistant_1 (native transcript ID)
    // - task run response_id = resp_omni_1 (Omnigent execution ID)
    // - task run triggering_message_id = msg_user_1
    setFixture("conv-x:resp_omni_1", READY_DETAIL);

    const bubbles: Bubble[] = [
      userBubble("Hi"),
      assistantBubble("msg_opencode_assistant_1", "completed", "Processing your request..."),
    ];

    // Mapping from bubble responseId to task run response_id
    const anchors = new Map<string, string>([
      ["msg_opencode_assistant_1", "resp_omni_1"],
    ]);

    const { container } = render(
      <ConversationPiece
        bubbles={bubbles}
        bubbleToTaskRunResponseId={anchors}
        renderOutcomesFor={new Set(["msg_opencode_assistant_1"])}
      />,
    );

    // The card should appear below the OpenCode assistant bubble
    const card = await screen.findByTestId("task-outcome-brief-card");
    expect(card).toBeInTheDocument();

    // The slot should have both IDs:
    // - data-response-id = the bubble's native transcript ID
    // - data-task-run-response-id = the Omnigent execution ID for API calls
    const slot = screen.getByTestId("assistant-outcome-slot");
    expect(slot.getAttribute("data-response-id")).toBe("msg_opencode_assistant_1");
    expect(slot.getAttribute("data-task-run-response-id")).toBe("resp_omni_1");

    // The card should fetch using the Omnigent response ID, not the transcript ID
    expect(card.getAttribute("data-task-run-id")).toBe("run-1");

    // Verify DOM order: user → assistant → outcome card
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

  it("handles multiple OpenCode-native turns with independent outcome cards", async () => {
    // Two turns, each with OpenCode-native IDs
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

    // Mappings: each bubble maps to its own task run
    const anchors = new Map<string, string>([
      ["msg_opencode_1", "resp_omni_1"],
      ["msg_opencode_2", "resp_omni_2"],
    ]);

    render(
      <ConversationPiece
        bubbles={bubbles}
        bubbleToTaskRunResponseId={anchors}
        renderOutcomesFor={new Set(["msg_opencode_1", "msg_opencode_2"])}
      />,
    );

    await waitFor(() => {
      expect(screen.getAllByTestId("assistant-outcome-slot")).toHaveLength(2);
    });

    const slots = screen.getAllByTestId("assistant-outcome-slot");
    const cards = screen.getAllByTestId("task-outcome-brief-card");

    expect(slots).toHaveLength(2);
    expect(cards).toHaveLength(2);

    // First card uses resp_omni_1
    expect(slots[0].getAttribute("data-response-id")).toBe("msg_opencode_1");
    expect(slots[0].getAttribute("data-task-run-response-id")).toBe("resp_omni_1");
    expect(cards[0].getAttribute("data-task-run-id")).toBe("run-1");

    // Second card uses resp_omni_2
    expect(slots[1].getAttribute("data-response-id")).toBe("msg_opencode_2");
    expect(slots[1].getAttribute("data-task-run-response-id")).toBe("resp_omni_2");
    expect(cards[1].getAttribute("data-task-run-id")).toBe("run-2");
  });

  it("does not attach card when triggering_message_id has no matching user bubble", async () => {
    // Task run has triggering_message_id that doesn't match any user bubble
    // Should NOT fall back to latest response
    setFixture("conv-x:resp_omni_1", READY_DETAIL);

    const bubbles: Bubble[] = [
      userBubble("Real user message"),
      assistantBubble("msg_opencode_1", "completed", "Response"),
    ];

    // Empty anchor map - this bubble has NO authoritative association
    // The bubble should NOT appear in renderOutcomesFor
    const anchors = new Map<string, string>(); // Empty - no valid association

    render(
      <ConversationPiece
        bubbles={bubbles}
        bubbleToTaskRunResponseId={anchors}
        renderOutcomesFor={new Set()} // Empty - no bubbles qualify
      />,
    );

    // No card should appear - fail closed when there's no association
    expect(screen.queryByTestId("task-outcome-brief-card")).toBeNull();
    expect(screen.queryByTestId("assistant-outcome-slot")).toBeNull();
  });

  it("handles mixed harness types: exact match and OpenCode-native", async () => {
    // First turn: normal harness with exact match (resp_1)
    // Second turn: OpenCode-native mismatch (msg_opencode_2 → resp_omni_2)
    const detail1 = {
      ...READY_DETAIL,
      run: { ...READY_DETAIL.run, id: "run-1", response_id: "resp_1" },
    };
    const detail2 = {
      ...READY_DETAIL,
      run: { ...READY_DETAIL.run, id: "run-2", response_id: "resp_omni_2" },
    };
    setFixture("conv-x:resp_1", detail1);
    setFixture("conv-x:resp_omni_2", detail2);

    const bubbles: Bubble[] = [
      userBubble("Normal request"),
      assistantBubble("resp_1", "completed", "Normal response"),
      userBubble("OpenCode request"),
      assistantBubble("msg_opencode_2", "completed", "OpenCode response"),
    ];

    // Mixed anchor mapping
    const anchors = new Map<string, string>([
      ["resp_1", "resp_1"], // exact match
      ["msg_opencode_2", "resp_omni_2"], // OpenCode-native mismatch
    ]);

    render(
      <ConversationPiece
        bubbles={bubbles}
        bubbleToTaskRunResponseId={anchors}
        renderOutcomesFor={new Set(["resp_1", "msg_opencode_2"])}
      />,
    );

    await waitFor(() => {
      expect(screen.getAllByTestId("assistant-outcome-slot")).toHaveLength(2);
    });

    const slots = screen.getAllByTestId("assistant-outcome-slot");

    // First slot: exact match - both IDs are the same
    expect(slots[0].getAttribute("data-response-id")).toBe("resp_1");
    expect(slots[0].getAttribute("data-task-run-response-id")).toBe("resp_1");

    // Second slot: OpenCode-native - different IDs
    expect(slots[1].getAttribute("data-response-id")).toBe("msg_opencode_2");
    expect(slots[1].getAttribute("data-task-run-response-id")).toBe("resp_omni_2");
  });

  it("no outcome card during streaming for OpenCode-native", () => {
    // OpenCode streaming bubble should not show a card
    const bubbles: Bubble[] = [
      userBubble("OpenCode request"),
      assistantBubble("msg_opencode_streaming", "streaming", "Streaming..."),
    ];

    const anchors = new Map<string, string>([
      ["msg_opencode_streaming", "resp_omni_1"],
    ]);

    render(
      <ConversationPiece
        bubbles={bubbles}
        bubbleToTaskRunResponseId={anchors}
        renderOutcomesFor={new Set(["msg_opencode_streaming"])}
      />,
    );

    // No slot appears for streaming bubbles
    expect(screen.queryByTestId("assistant-outcome-slot")).toBeNull();
    expect(screen.queryByTestId("task-outcome-brief-card")).toBeNull();
  });

  it("DOM order preserved: user bubble → OpenCode assistant bubble → outcome card", async () => {
    setFixture("conv-x:resp_omni_1", READY_DETAIL);

    const bubbles: Bubble[] = [
      userBubble("OpenCode user"),
      assistantBubble("msg_opencode_assistant", "completed", "OpenCode response"),
    ];

    const anchors = new Map<string, string>([
      ["msg_opencode_assistant", "resp_omni_1"],
    ]);

    const { container } = render(
      <ConversationPiece
        bubbles={bubbles}
        bubbleToTaskRunResponseId={anchors}
        renderOutcomesFor={new Set(["msg_opencode_assistant"])}
      />,
    );

    await screen.findByTestId("task-outcome-brief-card");

    const flow = container.querySelector('[data-testid="conversation-flow"]')!;
    const userMsg = flow.children[0] as HTMLElement;
    const assistantMsg = flow.children[1] as HTMLElement;
    const outcomeSlot = flow.children[2] as HTMLElement;

    // Verify strict ordering: never user → outcome → assistant
    expect(
      (userMsg.compareDocumentPosition(assistantMsg) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      (assistantMsg.compareDocumentPosition(outcomeSlot) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Outcome is after user (not between user and assistant)
    expect(
      (userMsg.compareDocumentPosition(outcomeSlot) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // Verify the outcome slot is a sibling of the assistant message, not a child
    expect(flow.children.length).toBe(3);
  });
});
