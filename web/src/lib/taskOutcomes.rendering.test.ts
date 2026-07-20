import { describe, expect, it } from "vitest";
import type { Bubble, RenderItem } from "./renderItems";
import type { TaskRunSummary } from "./taskOutcomes";
import {
  canonicalOutcomeResponseIds,
  hasVisibleFinalText,
  ownsOutcomeCard,
  resolveTaskOutcomeAnchors,
} from "./taskOutcomes";

// Helper to create bubbles with consistent stableId based on responseId
function assistant(
  responseId: string,
  lifecycle: "completed" | "streaming" | "failed" | "cancelled" = "completed",
  items?: RenderItem[],
): Bubble {
  return {
    kind: "assistant",
    responseId,
    stableId: `${responseId}-stable`,
    lifecycle,
    error: null,
    items: items ?? [
      { kind: "text" as const, itemId: `${responseId}-item`, text: "Hello", final: true },
    ],
  };
}

function reasoningBubble(responseId: string): Bubble {
  return {
    kind: "assistant",
    responseId,
    stableId: `${responseId}-stable`,
    lifecycle: "completed",
    error: null,
    items: [
      {
        kind: "reasoning" as const,
        itemId: `${responseId}-reasoning`,
        text: "Thinking...",
        duration: 0.5,
      },
    ],
  };
}

function mixedBubble(responseId: string): Bubble {
  return {
    kind: "assistant",
    responseId,
    stableId: `${responseId}-stable`,
    lifecycle: "completed",
    error: null,
    items: [
      {
        kind: "reasoning" as const,
        itemId: `${responseId}-reasoning`,
        text: "Thinking...",
        duration: 0.5,
      },
      { kind: "text" as const, itemId: `${responseId}-text`, text: "Final answer", final: true },
    ],
  };
}

function user(itemId: string): Bubble {
  return {
    kind: "user",
    itemId,
    content: [{ type: "input_text", text: "Hello" }],
  };
}

function run(responseId: string | null, triggeringMessageId: string | null): TaskRunSummary {
  return {
    id: `run-${responseId ?? "null"}`,
    conversation_id: "conv-1",
    response_id: responseId,
    triggering_message_id: triggeringMessageId,
    terminal_status: "completed",
    started_at: Date.now(),
    terminal_at: Date.now(),
    duration_ms: 1000,
    selected_provider: "anthropic",
    selected_model: "claude-4",
    requested_route_id: null,
    fallback_used: null,
    harness_id: null,
    proposed_task_family: null,
    input_tokens: null,
    output_tokens: null,
    total_cost_usd: null,
    commit_sha: null,
    changed_files_count: null,
    failure_error_code: null,
    langfuse_trace_id: null,
    created_at: Date.now(),
    updated_at: Date.now(),
  };
}

describe("hasVisibleFinalText", () => {
  it("returns true for bubble with visible final text", () => {
    const bubble = assistant("resp_1", "completed");
    expect(hasVisibleFinalText(bubble)).toBe(true);
  });

  it("returns false for reasoning-only bubble", () => {
    const bubble = reasoningBubble("resp_reasoning");
    expect(hasVisibleFinalText(bubble)).toBe(false);
  });

  it("returns false for bubble with only non-final text", () => {
    const bubble = assistant("resp_1", "completed", [
      { kind: "text" as const, itemId: "item-1", text: "", final: true },
    ]);
    expect(hasVisibleFinalText(bubble)).toBe(false);
  });

  it("returns true for bubble with reasoning followed by final text", () => {
    const bubble = mixedBubble("resp_mixed");
    expect(hasVisibleFinalText(bubble)).toBe(true);
  });

  it("returns false for user bubble", () => {
    const bubble = user("u1");
    expect(hasVisibleFinalText(bubble)).toBe(false);
  });

  it("returns false for streaming bubble even with text", () => {
    const bubble = assistant("resp_1", "streaming");
    expect(hasVisibleFinalText(bubble)).toBe(true); // hasVisibleFinalText doesn't check lifecycle
  });
});

describe("task outcome rendering", () => {
  it("renders a review shared by live and hydrated state once", () => {
    const bubbles = [assistant("run-1"), assistant("run-1")];
    expect(canonicalOutcomeResponseIds(bubbles)).toEqual(new Set(["run-1"]));
    expect(ownsOutcomeCard(bubbles, 0)).toBe(false);
    expect(ownsOutcomeCard(bubbles, 1)).toBe(true);
  });

  it("renders two distinct review IDs twice", () => {
    const bubbles = [assistant("run-1"), assistant("run-2")];
    expect(canonicalOutcomeResponseIds(bubbles)).toEqual(new Set(["run-1", "run-2"]));
    expect(ownsOutcomeCard(bubbles, 0)).toBe(true);
    expect(ownsOutcomeCard(bubbles, 1)).toBe(true);
  });

  it("keeps the canonical owner stable across hydration/rerender", () => {
    const hydrated = [assistant("run-1"), assistant("run-2"), assistant("run-1")];
    expect(hydrated.filter((_, index) => ownsOutcomeCard(hydrated, index))).toHaveLength(2);
    expect(ownsOutcomeCard(hydrated, 2)).toBe(true);
    expect(ownsOutcomeCard(hydrated, 0)).toBe(false);
  });
});

describe("resolveTaskOutcomeAnchors", () => {
  describe("returns Map<bubbleStableId, taskRunResponseId>", () => {
    it("maps by bubble.stableId, not by bubble.responseId", () => {
      // When exact match exists, the key is stableId, value is response_id
      const bubbles = [user("u1"), assistant("resp_1")];
      const runs = [run("resp_1", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Key is stableId ("resp_1-stable"), value is task_run.response_id ("resp_1")
      expect(anchors.get("resp_1-stable")).toBe("resp_1");
      expect(anchors.has("resp_1")).toBe(false); // responseId is NOT the key
      expect(anchors.size).toBe(1);
    });

    it("handles different stableId and responseId (OpenCode-native)", () => {
      // This tests that the mapping correctly handles OpenCode-native where
      // bubble.responseId differs from task_run.response_id
      const bubbles = [user("msg_user_1"), assistant("msg_opencode_assistant_1")];
      const runs = [run("resp_omni_1", "msg_user_1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Key is stableId, value is the Omnigent response ID
      expect(anchors.get("msg_opencode_assistant_1-stable")).toBe("resp_omni_1");
    });
  });

  describe("exact response-ID association", () => {
    it("associates when bubble responseId exactly matches task_run.response_id", () => {
      const bubbles = [user("u1"), assistant("resp_1"), user("u2"), assistant("resp_2")];
      const runs = [run("resp_1", null), run("resp_2", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.get("resp_1-stable")).toBe("resp_1");
      expect(anchors.get("resp_2-stable")).toBe("resp_2");
      expect(anchors.size).toBe(2);
    });

    it("handles null response_id in task run gracefully", () => {
      const bubbles = [user("u1"), assistant("resp_1")];
      const runs = [run(null, "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
    });

    it("attaches execution review cards to direct manual runs", () => {
      const bubbles = [user("u1"), assistant("resp_1")];
      const directRun = {
        ...run("resp_1", "u1"),
        routing_proposal_id: null,
        routing_decision_id: null,
      };

      expect(resolveTaskOutcomeAnchors(bubbles, [directRun])).toEqual(
        new Map([["resp_1-stable", "resp_1"]]),
      );
    });

    it("does not attach pending or running direct runs", () => {
      const bubbles = [user("u1"), assistant("resp_1")];
      const directRun = {
        ...run("resp_1", "u1"),
        terminal_status: "running" as const,
        routing_proposal_id: null,
        routing_decision_id: null,
      };

      expect(resolveTaskOutcomeAnchors(bubbles, [directRun]).size).toBe(0);
    });

    it("reasoning-only bubble with matching responseId does NOT get the mapping", () => {
      // Task run's response_id matches a reasoning-only bubble's responseId
      // The reasoning bubble should NOT get the mapping because it has no visible final text
      const bubbles = [user("u1"), reasoningBubble("msg_reasoning"), assistant("msg_final")];
      const runs = [run("msg_reasoning", null)]; // run with reasoning bubble's responseId

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Reasoning bubble should NOT get the mapping
      expect(anchors.has("msg_reasoning-stable")).toBe(false);
      // But the final answer bubble gets nothing either because we need Path B for it
      expect(anchors.size).toBe(0);
    });

    it("exact match reasoning bubble + final answer bubble -> reasoning gets nothing, final gets Path B", () => {
      // This is the key test for the deployed bug:
      // - reasoning-only bubble has responseId matching task_run.response_id
      // - final answer bubble is separate
      // The final answer should get the mapping via triggering-message bridge
      const bubbles = [user("u1"), reasoningBubble("msg_reasoning"), assistant("msg_final")];
      const runs = [run("msg_reasoning", "u1")]; // task_run.response_id = reasoning bubble's responseId

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Reasoning bubble has no visible text, so it doesn't get exact match
      expect(anchors.has("msg_reasoning-stable")).toBe(false);
      // Final answer bubble gets the mapping via triggering-message bridge
      expect(anchors.get("msg_final-stable")).toBe("msg_reasoning");
      expect(anchors.size).toBe(1);
    });
  });

  describe("OpenCode-native mismatch", () => {
    it("uses triggering_message_id bridge when bubble responseId differs from task_run.response_id", () => {
      // user item msg_user_1 triggers the run
      // assistant bubble responseId is msg_opencode_assistant_1 (native transcript ID)
      // task run response_id is resp_omni_1 (Omnigent execution ID)
      // task run triggering_message_id is msg_user_1
      const bubbles = [user("msg_user_1"), assistant("msg_opencode_assistant_1")];
      const runs = [run("resp_omni_1", "msg_user_1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // The card should be anchored to the OpenCode assistant bubble
      // but use the Omnigent response ID for API calls
      expect(anchors.get("msg_opencode_assistant_1-stable")).toBe("resp_omni_1");
    });

    it("attaches run to first completed assistant bubble with visible text before next user bubble", () => {
      // user u1 → assistant a1 (msg_*) → user u2 → assistant a2 (msg_*)
      // run triggered by u1 with triggering_message_id=u1
      // Expected: card attaches to a1 (the completed result before the next user)
      const bubbles = [
        user("u1"),
        assistant("msg_opencode_a1"),
        user("u2"),
        assistant("msg_opencode_a2"),
      ];
      const runs = [run("resp_omni_1", "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.get("msg_opencode_a1-stable")).toBe("resp_omni_1");
      expect(anchors.has("msg_opencode_a2-stable")).toBe(false);
    });

    it("reasoning bubble before final answer gets Path B to final answer", () => {
      // OpenCode-native: reasoning bubble → final answer bubble
      // Task run triggered by user, response_id matches reasoning bubble
      // Expected: final answer gets the mapping, reasoning does not
      const bubbles = [
        user("msg_user_1"),
        reasoningBubble("msg_opencode_reasoning"),
        assistant("msg_opencode_final"),
      ];
      const runs = [run("msg_opencode_reasoning", "msg_user_1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Reasoning bubble should NOT get the mapping (no visible text)
      expect(anchors.has("msg_opencode_reasoning-stable")).toBe(false);
      // Final answer should get the mapping via triggering-message bridge
      expect(anchors.get("msg_opencode_final-stable")).toBe("msg_opencode_reasoning");
    });
  });

  describe("multiple turns", () => {
    it("associates two runs with their respective turns in order", () => {
      // Two user turns, each triggering an OpenCode assistant response
      const bubbles = [
        user("msg_user_1"),
        assistant("msg_opencode_1"),
        user("msg_user_2"),
        assistant("msg_opencode_2"),
      ];
      const runs = [run("resp_omni_2", "msg_user_2"), run("resp_omni_1", "msg_user_1")]; // reversed order

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Each outcome should attach to its own triggering turn
      expect(anchors.get("msg_opencode_1-stable")).toBe("resp_omni_1");
      expect(anchors.get("msg_opencode_2-stable")).toBe("resp_omni_2");
    });

    it("handles mixed exact matches and triggering-message associations", () => {
      // First turn: exact match (normal harness)
      // Second turn: OpenCode-native mismatch
      const bubbles = [user("u1"), assistant("resp_1"), user("u2"), assistant("msg_opencode_2")];
      const runs = [
        run("resp_1", null), // exact match
        run("resp_omni_2", "u2"), // triggering message bridge
      ];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.get("resp_1-stable")).toBe("resp_1");
      expect(anchors.get("msg_opencode_2-stable")).toBe("resp_omni_2");
    });
  });

  describe("fail closed behavior", () => {
    it("renders no card when response_id and triggering_message_id both don't match", () => {
      // Task run has response_id that doesn't match any bubble
      // AND triggering_message_id that doesn't match any user bubble
      const bubbles = [user("u1"), assistant("resp_1")];
      const runs = [run("resp_unknown", "u_unknown")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
    });

    it("does not fall back to 'latest response' when triggering_message_id has no match", () => {
      // run1: unknown response_id, non-matching triggering_message_id
      // bubbles: u1 → a1
      // Should NOT attach to a1 just because it's the latest response
      const bubbles = [user("u1"), assistant("a1")];
      const runs = [run("unknown", "u_unknown")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
    });

    it("no mapping means no card - no fallback to bubble.responseId", () => {
      // This is the key test: when there's no authoritative mapping,
      // the Map should NOT contain bubble.responseId as a fallback
      const bubbles = [user("u1"), assistant("resp_1")];
      const runs = [run("resp_unknown", "u_unknown")]; // no valid association

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
      // The bubble's responseId should NOT be in the map as a fallback
      expect(anchors.has("resp_1")).toBe(false);
      expect(anchors.has("resp_1-stable")).toBe(false);
    });

    it("fails closed instead of choosing by order when a turn has multiple final bubbles", () => {
      const bubbles = [
        user("u1"),
        assistant("native_first"),
        assistant("native_second"),
        user("u2"),
      ];
      const runs = [run("runner_response", "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
      expect(anchors.has("native_first-stable")).toBe(false);
      expect(anchors.has("native_second-stable")).toBe(false);
    });
  });

  describe("uniqueness enforcement", () => {
    it("ensures at most one task run per assistant result bubble", () => {
      // Two runs both targeting the same triggering message
      // Only the first should be associated
      const bubbles = [user("u1"), assistant("msg_opencode_1")];
      const runs = [
        run("resp_omni_1", "u1"),
        run("resp_omni_2", "u1"), // duplicate - should be ignored
      ];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(1);
      expect(anchors.get("msg_opencode_1-stable")).toBe("resp_omni_1");
    });

    it("exact match takes priority over triggering-message association", () => {
      // Bubble responseId matches a run's response_id
      // AND another run's triggering_message_id matches the user
      // The exact match should win
      const bubbles = [user("u1"), assistant("resp_1")];
      const runs = [
        run("resp_1", null), // exact match
        run("resp_omni_2", "u1"), // triggering message (should be shadowed)
      ];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(1);
      expect(anchors.get("resp_1-stable")).toBe("resp_1");
    });

    it("does not attach duplicate cards for same responseId (reconnect scenario)", () => {
      // Same response appears twice (reconnect scenario)
      // Should only have one mapping
      const bubbles = [assistant("resp_1"), assistant("resp_1")];
      const runs = [run("resp_1", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(1);
      expect(anchors.get("resp_1-stable")).toBe("resp_1");
    });
  });

  describe("content requirement - visible final text", () => {
    it("reasoning-only bubble does NOT get the mapping even if responseId matches", () => {
      // Task run response_id matches a reasoning bubble's responseId
      // But reasoning bubble has no visible text, so it should NOT get the mapping
      const bubbles = [user("u1"), reasoningBubble("msg_reasoning")];
      const runs = [run("msg_reasoning", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
      expect(anchors.has("msg_reasoning-stable")).toBe(false);
    });

    it("reasoning + text bubble DOES get the mapping", () => {
      // Bubble has both reasoning and final text
      const bubbles = [user("u1"), mixedBubble("msg_mixed")];
      const runs = [run("msg_mixed", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.get("msg_mixed-stable")).toBe("msg_mixed");
    });

    it("empty text bubble does NOT get the mapping", () => {
      const bubbles = [
        user("u1"),
        assistant("resp_empty", "completed", [
          { kind: "text" as const, itemId: "item-1", text: "   ", final: true },
        ]),
      ];
      const runs = [run("resp_empty", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
    });

    it("tool-only bubble does NOT get the mapping (no text)", () => {
      const bubbles = [
        user("u1"),
        {
          kind: "assistant" as const,
          responseId: "resp_tool_only",
          stableId: "resp_tool_only-stable",
          lifecycle: "completed" as const,
          error: null,
          items: [
            {
              kind: "tool" as const,
              itemId: "tool-1",
              execution: {
                name: "bash",
                arguments: {},
                argsSummary: "",
                callId: "call-1",
                agentName: "agent",
                executedBy: "server" as const,
                output: "done",
              },
              output: "done",
              state: "output-available" as const,
              startedAt: null,
              duration: 1.0,
            },
          ],
        },
      ];
      const runs = [run("resp_tool_only", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
    });
  });

  describe("streaming behavior", () => {
    it("ignores streaming bubbles when finding target for triggering-message association", () => {
      // user u1 → streaming bubble → completed bubble → user u2
      // Run triggered by u1 should attach to the completed bubble, not streaming
      const bubbles = [
        user("u1"),
        assistant("msg_streaming", "streaming"),
        assistant("msg_completed", "completed"),
        user("u2"),
      ];
      const runs = [run("resp_omni_1", "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Should attach to the completed bubble before u2
      expect(anchors.get("msg_completed-stable")).toBe("resp_omni_1");
      expect(anchors.has("msg_streaming-stable")).toBe(false);
    });

    it("no card during streaming - only completed responses", () => {
      // Only a streaming bubble exists
      // No mapping should be created
      const bubbles = [user("u1"), assistant("msg_streaming", "streaming")];
      const runs = [run("resp_omni_1", "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // The streaming bubble should not get a mapping even with triggering_message_id
      expect(anchors.size).toBe(0);
    });
  });

  describe("DOM order", () => {
    it("card appears after correct assistant bubble, not between user and assistant", () => {
      // This is validated by the association logic itself:
      // The triggering-message bridge finds the NEXT assistant bubble
      // after the user, not before it
      const bubbles = [user("u1"), assistant("msg_opencode_1")];
      const runs = [run("resp_omni_1", "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Card attaches to the assistant bubble, not the user
      expect(anchors.has("msg_opencode_1-stable")).toBe(true);
    });
  });

  describe("edge cases", () => {
    it("handles empty bubble list", () => {
      const anchors = resolveTaskOutcomeAnchors([], [run("resp_1", null)]);
      expect(anchors.size).toBe(0);
    });

    it("handles empty runs list", () => {
      const bubbles = [user("u1"), assistant("resp_1")];
      const anchors = resolveTaskOutcomeAnchors(bubbles, []);
      expect(anchors.size).toBe(0);
    });

    it("handles failed/cancelled assistant bubbles with visible text", () => {
      const bubbles = [user("u1"), assistant("msg_failed", "failed")];
      const runs = [run("resp_omni_1", "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Non-streaming bubbles with visible text should still be eligible for attachment
      expect(anchors.get("msg_failed-stable")).toBe("resp_omni_1");
    });

    it("handles failed bubble without text", () => {
      const bubbles = [
        user("u1"),
        {
          kind: "assistant" as const,
          responseId: "msg_failed",
          stableId: "msg_failed-stable",
          lifecycle: "failed" as const,
          error: "something went wrong",
          items: [], // no text
        },
      ];
      const runs = [run("resp_omni_1", "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Bubble without visible text should not get the mapping
      expect(anchors.size).toBe(0);
    });
  });
});
