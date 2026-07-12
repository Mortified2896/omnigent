import { describe, expect, it } from "vitest";
import type { Bubble } from "./renderItems";
import type { TaskRunSummary } from "./taskOutcomes";
import { canonicalOutcomeResponseIds, ownsOutcomeCard, resolveTaskOutcomeAnchors } from "./taskOutcomes";

function assistant(
  responseId: string,
  lifecycle: "completed" | "streaming" | "failed" | "cancelled" = "completed",
): Bubble {
  return {
    kind: "assistant",
    responseId,
    stableId: `${responseId}-stable`,
    lifecycle,
    error: null,
    items: [{ kind: "text", itemId: `${responseId}-item`, text: "Hello", final: true }],
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
  describe("exact response-ID association", () => {
    it("associates when bubble responseId exactly matches task_run.response_id", () => {
      const bubbles = [user("u1"), assistant("resp_1"), user("u2"), assistant("resp_2")];
      const runs = [run("resp_1", null), run("resp_2", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.get("resp_1")).toBe("resp_1");
      expect(anchors.get("resp_2")).toBe("resp_2");
      expect(anchors.size).toBe(2);
    });

    it("handles null response_id in task run gracefully", () => {
      const bubbles = [user("u1"), assistant("resp_1")];
      const runs = [run(null, "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(0);
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
      expect(anchors.get("msg_opencode_assistant_1")).toBe("resp_omni_1");
    });

    it("attaches run to final completed assistant bubble before next user bubble", () => {
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

      expect(anchors.get("msg_opencode_a1")).toBe("resp_omni_1");
      expect(anchors.has("msg_opencode_a2")).toBe(false);
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
      expect(anchors.get("msg_opencode_1")).toBe("resp_omni_1");
      expect(anchors.get("msg_opencode_2")).toBe("resp_omni_2");
    });

    it("handles mixed exact matches and triggering-message associations", () => {
      // First turn: exact match (normal harness)
      // Second turn: OpenCode-native mismatch
      const bubbles = [
        user("u1"),
        assistant("resp_1"),
        user("u2"),
        assistant("msg_opencode_2"),
      ];
      const runs = [
        run("resp_1", null), // exact match
        run("resp_omni_2", "u2"), // triggering message bridge
      ];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.get("resp_1")).toBe("resp_1");
      expect(anchors.get("msg_opencode_2")).toBe("resp_omni_2");
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
      expect(anchors.get("msg_opencode_1")).toBe("resp_omni_1");
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
      expect(anchors.get("resp_1")).toBe("resp_1");
    });

    it("does not attach duplicate cards for same responseId", () => {
      // Same response appears twice (reconnect scenario)
      // Should only have one mapping
      const bubbles = [assistant("resp_1"), assistant("resp_1")];
      const runs = [run("resp_1", null)];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      expect(anchors.size).toBe(1);
      expect(anchors.get("resp_1")).toBe("resp_1");
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
      expect(anchors.get("msg_completed")).toBe("resp_omni_1");
      expect(anchors.has("msg_streaming")).toBe(false);
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
      expect(anchors.has("msg_opencode_1")).toBe(true);
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

    it("handles failed/cancelled assistant bubbles", () => {
      const bubbles = [user("u1"), assistant("msg_failed", "failed")];
      const runs = [run("resp_omni_1", "u1")];

      const anchors = resolveTaskOutcomeAnchors(bubbles, runs);

      // Non-streaming bubbles should still be eligible for attachment
      expect(anchors.get("msg_failed")).toBe("resp_omni_1");
    });
  });
});
