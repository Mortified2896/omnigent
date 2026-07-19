import { describe, expect, it } from "vitest";
import type { Bubble } from "./renderItems";
import {
  normalizeRoutingTurn,
  reconcileRoutingTurnBubbles,
  type RoutingTurnRecord,
} from "./routingTurns";

const PROPOSAL = {
  omniroute_route_id: "auto/coding",
  reasoning_effort: "medium",
  permission_mode: "default",
};

function liveRouteBubble(
  elicitationId = "route_1",
  overrides: Partial<Extract<Bubble, { kind: "assistant" }>> = {},
): Bubble {
  return {
    kind: "assistant",
    responseId: "resp_1",
    stableId: "resp_1:0",
    lifecycle: "completed",
    error: null,
    items: [
      {
        kind: "elicitation",
        itemId: "item_1",
        elicitationId,
        message: "Approve route",
        phase: "route_approval",
        policyName: "model_routing_agent",
        contentPreview: "",
        requestedSchema: {},
        status: "pending",
        response: null,
        routeProposal: PROPOSAL,
      },
    ],
    ...overrides,
  };
}

function user(itemId: string, text = itemId): Bubble {
  return {
    kind: "user",
    itemId,
    stableKey: itemId,
    content: [{ type: "input_text", text }],
  };
}

function assistant(responseId: string, text = responseId): Bubble {
  return {
    kind: "assistant",
    responseId,
    stableId: responseId,
    lifecycle: "completed",
    error: null,
    items: [{ kind: "text", itemId: responseId, text, final: true }],
  };
}

function record(overrides: Partial<RoutingTurnRecord> = {}): RoutingTurnRecord {
  return {
    id: "turn_1",
    elicitation_id: "route_1",
    response_id: "resp_1",
    status: "responded",
    proposal: PROPOSAL,
    response: { action: "accept" },
    ...overrides,
  };
}

describe("routing turn hydration", () => {
  it("normalizes alternate backend envelope fields and action vocabulary", () => {
    for (const action of ["accept", "approved", "changed"]) {
      expect(
        normalizeRoutingTurn({
          id: "turn_1",
          elicitation_id: "route_1",
          status: "resolved",
          original_proposal: PROPOSAL,
          action,
          final_selection: { omniroute_route_id: "auto/easy" },
        }),
      ).toMatchObject({
        status: "responded",
        response: {
          action: "accept",
          content: { final_selection: { omniroute_route_id: "auto/easy" } },
        },
      });
    }
    expect(
      normalizeRoutingTurn({
        id: "turn_2",
        elicitation_id: "route_2",
        proposal: PROPOSAL,
        action: "declined",
      }),
    ).toMatchObject({ response: { action: "decline" } });
  });

  it("uses the persisted record over live duplicates", () => {
    const bubbles = reconcileRoutingTurnBubbles(
      [liveRouteBubble(), liveRouteBubble()],
      [record()],
      true,
    );
    const routeItems = bubbles.flatMap((bubble) =>
      bubble.kind === "assistant"
        ? bubble.items.filter((item) => item.kind === "elicitation" && item.routeProposal)
        : [],
    );
    expect(routeItems).toHaveLength(1);
    expect(routeItems[0]).toMatchObject({ status: "responded", response: { action: "accept" } });
  });

  it("preserves unmatched live route cards when hydration is loading or stale", () => {
    for (const loaded of [false, true]) {
      const bubbles = reconcileRoutingTurnBubbles([liveRouteBubble()], [], loaded);
      expect(bubbles[0]).toMatchObject({
        kind: "assistant",
        items: [{ kind: "elicitation", elicitationId: "route_1", status: "pending" }],
      });
    }
  });

  it("hydrates a missing transcript card exactly once", () => {
    const bubbles = reconcileRoutingTurnBubbles([], [record()], true);
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0]).toMatchObject({
      kind: "assistant",
      stableId: "routing-turn:turn_1",
      items: [{ kind: "elicitation", elicitationId: "route_1" }],
    });
  });

  it("places missing-trigger records over only unmatched records and user turns", () => {
    const records = [
      record({
        id: "turn_1",
        elicitation_id: "route_1",
        response_id: "resp_1",
        created_at: 1,
      }),
      record({
        id: "turn_2",
        elicitation_id: "route_2",
        response_id: "resp_2",
        created_at: 2,
      }),
      record({
        id: "turn_3",
        elicitation_id: "route_3",
        response_id: "resp_3",
        created_at: 3,
      }),
    ];
    const bubbles = reconcileRoutingTurnBubbles(
      [
        user("u1"),
        liveRouteBubble("route_1"),
        assistant("resp_1"),
        user("u2"),
        assistant("native_2"),
        user("u3"),
        assistant("native_3"),
      ],
      records,
      true,
    );

    const order = bubbles.flatMap((bubble) => {
      if (bubble.kind === "user") return [bubble.itemId];
      if (bubble.kind === "assistant") {
        const elicitation = bubble.items.find((item) => item.kind === "elicitation");
        return [elicitation?.elicitationId ?? bubble.responseId];
      }
      return [];
    });
    expect(order).toEqual([
      "u1",
      "route_1",
      "resp_1",
      "u2",
      "route_2",
      "native_2",
      "u3",
      "route_3",
      "native_3",
    ]);
  });

  it("resolves response linkage before deterministic ordinal fallback", () => {
    const bubbles = reconcileRoutingTurnBubbles(
      [user("u1"), assistant("resp_1"), user("u2"), assistant("resp_2")],
      [
        record({
          id: "turn_b",
          elicitation_id: "route_b",
          response_id: null,
          created_at: 10,
        }),
        record({
          id: "turn_a",
          elicitation_id: "route_a",
          response_id: "resp_1",
          created_at: 10,
        }),
      ],
      true,
    );
    const order = bubbles.flatMap((bubble) => {
      if (bubble.kind === "user") return [bubble.itemId];
      if (bubble.kind === "assistant") {
        const elicitation = bubble.items.find((item) => item.kind === "elicitation");
        return [elicitation?.elicitationId ?? bubble.responseId];
      }
      return [];
    });
    expect(order).toEqual(["u1", "route_a", "resp_1", "u2", "route_b", "resp_2"]);
  });

  it("keeps a stale explicit trigger trailing rather than guessing another prompt", () => {
    const bubbles = reconcileRoutingTurnBubbles(
      [user("u1"), assistant("resp_1")],
      [record({ triggering_message_id: "missing", response_id: "unknown" })],
      true,
    );
    expect(bubbles.at(-1)).toMatchObject({ stableId: "routing-turn:turn_1" });
  });

  it("orders identical or missing timestamps by durable ids", () => {
    const bubbles = reconcileRoutingTurnBubbles(
      [user("u1"), assistant("a1"), user("u2"), assistant("a2")],
      [
        record({ id: "turn_b", elicitation_id: "route_b", response_id: null }),
        record({ id: "turn_a", elicitation_id: "route_a", response_id: null }),
      ],
      true,
    );
    const hydrated = bubbles.flatMap((bubble) =>
      bubble.kind === "assistant"
        ? bubble.items.flatMap((item) => (item.kind === "elicitation" ? [item.elicitationId] : []))
        : [],
    );
    expect(hydrated).toEqual(["route_a", "route_b"]);
  });

  it("places a hydrated decision between its user prompt and execution response", () => {
    const user: Bubble = {
      kind: "user",
      itemId: "msg_user",
      stableKey: "msg_user",
      content: [{ type: "input_text", text: "Inspect the repository" }],
    };
    const response: Bubble = {
      kind: "assistant",
      responseId: "msg_response",
      stableId: "msg_response",
      lifecycle: "completed",
      error: null,
      items: [{ kind: "text", itemId: "msg_response", text: "Repository is clean", final: true }],
    };

    const bubbles = reconcileRoutingTurnBubbles(
      [user, response],
      [record({ triggering_message_id: "msg_user" })],
      true,
    );

    expect(bubbles.map((bubble) => bubble.kind)).toEqual(["user", "assistant", "assistant"]);
    expect(bubbles[1]).toMatchObject({ stableId: "routing-turn:turn_1" });
    expect(bubbles[2]).toMatchObject({ stableId: "msg_response" });
  });
});
