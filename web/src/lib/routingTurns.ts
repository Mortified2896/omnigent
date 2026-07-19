import { useEffect, useRef, useState } from "react";
import { authenticatedFetch } from "./identity";
import type { Bubble, RenderItem } from "./renderItems";

export interface RoutingTurnRecord {
  id: string;
  elicitation_id: string;
  response_id?: string | null;
  triggering_message_id?: string | null;
  status: "pending" | "responded";
  proposal: Record<string, unknown>;
  response: {
    action: "accept" | "decline" | "cancel" | "auto_resolved";
    content?: Record<string, unknown>;
  } | null;
  created_at?: number;
}

interface RoutingTurnListEnvelope {
  turns?: unknown;
  routing_turns?: unknown;
  data?: unknown;
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function normalizeAction(
  value: unknown,
): NonNullable<RoutingTurnRecord["response"]>["action"] | null {
  if (typeof value !== "string") return null;
  const action = value.trim().toLowerCase().replaceAll("-", "_");
  if (["accept", "accepted", "approve", "approved", "adjusted", "changed"].includes(action)) {
    return "accept";
  }
  if (["decline", "declined", "reject", "rejected"].includes(action)) return "decline";
  if (["cancel", "cancelled", "canceled"].includes(action)) return "cancel";
  if (["auto_resolved", "resolved_elsewhere"].includes(action)) return "auto_resolved";
  return null;
}

export function normalizeRoutingTurn(value: unknown): RoutingTurnRecord | null {
  const row = object(value);
  if (!row) return null;
  const id = typeof row.id === "string" ? row.id : "";
  const elicitationId =
    typeof row.elicitation_id === "string"
      ? row.elicitation_id
      : typeof row.elicitationId === "string"
        ? row.elicitationId
        : "";
  const proposal =
    object(row.proposal) ?? object(row.route_proposal) ?? object(row.original_proposal);
  if (!id || !elicitationId || !proposal) return null;

  const responseObject = object(row.response);
  const action = normalizeAction(responseObject?.action ?? row.action);
  const finalSelection =
    object(row.final_selection) ?? object(row.route_selection) ?? object(row.adjustment);
  const content =
    object(responseObject?.content) ??
    (finalSelection ? { final_selection: finalSelection } : undefined);
  const rawStatus = row.status;
  const status: RoutingTurnRecord["status"] =
    rawStatus === "responded" || rawStatus === "resolved" || action ? "responded" : "pending";
  const response =
    status === "responded" && action ? { action, ...(content ? { content } : {}) } : null;

  return {
    id,
    elicitation_id: elicitationId,
    response_id: typeof row.response_id === "string" ? row.response_id : null,
    triggering_message_id:
      typeof row.triggering_message_id === "string" ? row.triggering_message_id : null,
    status,
    proposal,
    response,
    created_at: typeof row.created_at === "number" ? row.created_at : undefined,
  };
}

export async function listSessionRoutingTurns(
  sessionId: string,
  signal?: AbortSignal,
): Promise<RoutingTurnRecord[]> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/routing-turns`,
    { credentials: "same-origin", ...(signal ? { signal } : {}) },
  );
  if (!response.ok) throw new Error(`listSessionRoutingTurns failed: ${response.status}`);
  const payload: unknown = await response.json();
  const envelope = object(payload) as RoutingTurnListEnvelope | null;
  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray(envelope?.turns)
      ? envelope.turns
      : Array.isArray(envelope?.routing_turns)
        ? envelope.routing_turns
        : Array.isArray(envelope?.data)
          ? envelope.data
          : [];
  return rows.flatMap((row) => {
    const normalized = normalizeRoutingTurn(row);
    return normalized ? [normalized] : [];
  });
}

export function useRoutingTurns(
  sessionId: string | null | undefined,
  streamStatus: "idle" | "streaming",
): { loaded: boolean; turns: readonly RoutingTurnRecord[] } {
  const [state, setState] = useState<{
    sessionId: string | null;
    loaded: boolean;
    turns: readonly RoutingTurnRecord[];
  }>({ sessionId: null, loaded: false, turns: [] });
  const previousStatus = useRef(streamStatus);

  useEffect(() => {
    const controller = new AbortController();
    if (!sessionId) {
      setState({ sessionId: null, loaded: true, turns: [] });
      return () => controller.abort();
    }
    setState({ sessionId, loaded: false, turns: [] });
    void listSessionRoutingTurns(sessionId, controller.signal).then(
      (turns) => {
        if (!controller.signal.aborted) setState({ sessionId, loaded: true, turns });
      },
      (error) => {
        if (!controller.signal.aborted) {
          console.warn("Failed to hydrate routing turns", error);
          setState({ sessionId, loaded: true, turns: [] });
        }
      },
    );
    return () => controller.abort();
  }, [sessionId]);

  useEffect(() => {
    const wasStreaming = previousStatus.current === "streaming";
    previousStatus.current = streamStatus;
    if (!sessionId || !wasStreaming || streamStatus !== "idle") return;
    const controller = new AbortController();
    void listSessionRoutingTurns(sessionId, controller.signal).then(
      (turns) => {
        if (!controller.signal.aborted) setState({ sessionId, loaded: true, turns });
      },
      (error) => {
        if (!controller.signal.aborted) console.warn("Failed to refresh routing turns", error);
      },
    );
    return () => controller.abort();
  }, [sessionId, streamStatus]);

  return state.sessionId === (sessionId ?? null)
    ? { loaded: state.loaded, turns: state.turns }
    : { loaded: false, turns: [] };
}

function turnItem(turn: RoutingTurnRecord): Extract<RenderItem, { kind: "elicitation" }> {
  return {
    kind: "elicitation",
    itemId: turn.response_id ?? turn.id,
    elicitationId: turn.elicitation_id,
    message: "Approve Omnigent Model Routing Agent recommendation before execution.",
    phase: "route_approval",
    policyName: "model_routing_agent",
    contentPreview: "",
    requestedSchema: {},
    status: turn.status,
    response: turn.response,
    routeProposal: turn.proposal,
  };
}

/** Reconcile transient transcript cards with persisted routing records. */
export function reconcileRoutingTurnBubbles(
  bubbles: readonly Bubble[],
  records: readonly RoutingTurnRecord[],
  loaded: boolean,
): Bubble[] {
  const byElicitation = new Map(records.map((record) => [record.elicitation_id, record]));
  const consumed = new Set<string>();
  const livePositions = new Map<string, number>();
  const reconciled: Bubble[] = [];

  for (const bubble of bubbles) {
    if (bubble.kind !== "assistant") {
      reconciled.push(bubble);
      continue;
    }
    let changed = false;
    const items = bubble.items.flatMap((item) => {
      if (item.kind !== "elicitation" || !item.routeProposal) return [item];
      const record = byElicitation.get(item.elicitationId);
      if (!record) return [item];
      if (consumed.has(item.elicitationId)) {
        changed = true;
        return [];
      }
      consumed.add(record.elicitation_id);
      livePositions.set(record.elicitation_id, reconciled.length);
      changed = true;
      return [turnItem(record)];
    });
    reconciled.push(changed ? { ...bubble, items } : bubble);
  }

  if (!loaded) return reconciled;
  const sorted = [...records].sort((a, b) => {
    const aCreated = a.created_at;
    const bCreated = b.created_at;
    if (aCreated !== undefined && bCreated !== undefined && aCreated !== bCreated) {
      return aCreated - bCreated;
    }
    if (aCreated === undefined && bCreated !== undefined) return 1;
    if (aCreated !== undefined && bCreated === undefined) return -1;
    return a.id.localeCompare(b.id) || a.elicitation_id.localeCompare(b.elicitation_id);
  });
  const userIndexes = reconciled.flatMap((bubble, index) =>
    bubble.kind === "user" ? [index] : [],
  );
  const claimedUsers = new Set<number>();
  const insertAfter = new Map<number, RoutingTurnRecord[]>();
  const trailing: RoutingTurnRecord[] = [];
  const assigned = new Set(consumed);

  const exactUserIndex = (record: RoutingTurnRecord): number =>
    record.triggering_message_id == null
      ? -1
      : reconciled.findIndex(
          (bubble) => bubble.kind === "user" && bubble.itemId === record.triggering_message_id,
        );
  const responseUserIndex = (record: RoutingTurnRecord): number => {
    if (record.response_id == null) return -1;
    const responseIndex = reconciled.findIndex(
      (bubble) =>
        bubble.kind === "assistant" &&
        bubble.items.every((item) => item.kind !== "elicitation") &&
        (bubble.responseId === record.response_id || bubble.stableId === record.response_id),
    );
    if (responseIndex < 0) return -1;
    let nearest = -1;
    for (const index of userIndexes) {
      if (index < responseIndex) nearest = index;
    }
    return nearest === -1 ? -1 : nearest;
  };
  const assign = (record: RoutingTurnRecord, userIndex: number): boolean => {
    if (userIndex < 0 || claimedUsers.has(userIndex)) return false;
    claimedUsers.add(userIndex);
    assigned.add(record.elicitation_id);
    insertAfter.set(userIndex, [...(insertAfter.get(userIndex) ?? []), record]);
    return true;
  };

  // Existing live cards stay exactly where the stream placed them, but their
  // user turns are reserved so ordinal hydration cannot reuse those prompts.
  for (const record of sorted) {
    if (!consumed.has(record.elicitation_id)) continue;
    const exact = exactUserIndex(record);
    if (exact >= 0) {
      claimedUsers.add(exact);
      continue;
    }
    const liveIndex = livePositions.get(record.elicitation_id);
    const preceding =
      liveIndex === undefined
        ? undefined
        : (() => {
            let nearest: number | undefined;
            for (const index of userIndexes) {
              if (index < liveIndex) nearest = index;
            }
            return nearest;
          })();
    if (preceding !== undefined) claimedUsers.add(preceding);
  }

  // Strong identifiers win globally before any ordinal slot is consumed.
  for (const record of sorted) {
    if (assigned.has(record.elicitation_id)) continue;
    const exact = exactUserIndex(record);
    if (exact >= 0) {
      if (!assign(record, exact)) trailing.push(record);
      continue;
    }
    const linked = responseUserIndex(record);
    if (linked >= 0) {
      if (!assign(record, linked)) trailing.push(record);
      continue;
    }
    // A present but stale trigger is stronger than an ordinal guess. Keep it
    // trailing rather than attaching the card to a different user prompt.
    if (record.triggering_message_id != null) {
      assigned.add(record.elicitation_id);
      trailing.push(record);
    }
  }

  const unmatchedRecords = sorted.filter((record) => !assigned.has(record.elicitation_id));
  const unmatchedUsers = userIndexes.filter((index) => !claimedUsers.has(index));
  unmatchedRecords.forEach((record, ordinal) => {
    const target = unmatchedUsers[ordinal];
    if (target === undefined) {
      trailing.push(record);
      assigned.add(record.elicitation_id);
    } else {
      assign(record, target);
    }
  });

  const durableBubble = (record: RoutingTurnRecord): Bubble => ({
    kind: "assistant",
    responseId: record.response_id ?? `routing-turn:${record.id}`,
    stableId: `routing-turn:${record.id}`,
    lifecycle: "completed",
    error: null,
    items: [turnItem(record)],
  });
  const placed: Bubble[] = [];
  reconciled.forEach((bubble, index) => {
    placed.push(bubble);
    for (const record of insertAfter.get(index) ?? []) placed.push(durableBubble(record));
  });
  placed.push(...trailing.map(durableBubble));
  return placed;
}
