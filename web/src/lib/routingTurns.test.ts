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

function liveRouteBubble(elicitationId = "route_1"): Bubble {
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
