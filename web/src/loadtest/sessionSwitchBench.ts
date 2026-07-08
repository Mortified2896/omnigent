// Session-switch benchmark: measures how long until the chat surface is
// interactive after `switchTo`, with controlled fetch delays on the history
// and snapshot endpoints. Mirrors the vitest fetch shims in chatStore.test.ts
// but reports wall-clock milestones the PR optimizes:
//
//   - firstBubbleMs: loadingConversation cleared + blocks hydrated (TTFP proxy)
//   - metadataLagMs: time from firstBubble until boundAgentId is set
//   - totalBindMs: switchTo promise settled

import { QueryClient } from "@tanstack/react-query";
import { vi } from "vitest";
import type { ConversationItem } from "@/lib/conversationItems";
import { SESSION_HISTORY_PAGE_SIZE, prefetchSessionForSwitch } from "@/lib/sessionsApi";
import { initChatStore, useChatStore } from "@/store/chatStore";

export interface SessionSwitchBenchResult {
  scenario: string;
  historyDelayMs: number;
  snapshotDelayMs: number;
  prefetch: boolean;
  /** switchTo start → store history hydrated (`!loadingConversation && blocks`). */
  firstBubbleMs: number;
  historyHydratedMs: number;
  /** firstBubble → boundAgentId populated. */
  metadataLagMs: number;
  /** switchTo start → bindStream fully settled. */
  totalBindMs: number;
  /** Observed history endpoint delay (sanity). */
  historyFetchMs: number;
  /** Observed snapshot endpoint delay (sanity). */
  snapshotFetchMs: number;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function mockJson(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => body,
    text: async () => JSON.stringify(body),
    body: null,
  } as unknown as Response;
}

function emptyStream(): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(enc.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });
}

function streamResponse(): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => null,
    text: async () => "",
    body: emptyStream(),
  } as unknown as Response;
}

function userMessage(responseId: string, text: string): ConversationItem {
  return {
    id: `msg_${responseId}_user`,
    response_id: responseId,
    type: "message",
    role: "user",
    status: "completed",
    content: [{ type: "input_text", text }],
  };
}

function assistantMessage(responseId: string, text: string): ConversationItem {
  return {
    id: `msg_${responseId}_asst`,
    response_id: responseId,
    type: "message",
    role: "assistant",
    status: "completed",
    model: "bench-agent",
    content: [{ type: "output_text", text }],
  };
}

/** Build `count` user+assistant turn pairs for history hydration. */
export function buildTranscript(turns: number): ConversationItem[] {
  const items: ConversationItem[] = [];
  for (let i = 0; i < turns; i += 1) {
    const rid = `resp_${i.toString().padStart(3, "0")}`;
    items.push(userMessage(rid, `question ${i}`));
    items.push(assistantMessage(rid, `answer ${i}`));
  }
  return items;
}

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx]!;
}

export function summarizeRuns(runs: SessionSwitchBenchResult[]): SessionSwitchBenchResult & {
  runs: number;
} {
  const pick = (key: keyof SessionSwitchBenchResult): number => {
    const vals = runs.map((r) => r[key] as number).sort((a, b) => a - b);
    return percentile(vals, 50);
  };
  const first = runs[0]!;
  return {
    scenario: first.scenario,
    historyDelayMs: first.historyDelayMs,
    snapshotDelayMs: first.snapshotDelayMs,
    prefetch: first.prefetch,
    firstBubbleMs: pick("firstBubbleMs"),
    historyHydratedMs: pick("historyHydratedMs"),
    metadataLagMs: pick("metadataLagMs"),
    totalBindMs: pick("totalBindMs"),
    historyFetchMs: pick("historyFetchMs"),
    snapshotFetchMs: pick("snapshotFetchMs"),
    runs: runs.length,
  };
}

/**
 * Run one session-switch timing measurement with injected network delays.
 */
export async function runSessionSwitchBench(opts: {
  sessionId?: string;
  turns?: number;
  historyDelayMs: number;
  snapshotDelayMs: number;
  prefetch?: boolean;
  /** Pause after prefetch starts, before switchTo (sidebar hover + read). */
  humanDelayMs?: number;
  scenario?: string;
}): Promise<SessionSwitchBenchResult> {
  const sessionId = opts.sessionId ?? "bench_conv";
  const turns = opts.turns ?? SESSION_HISTORY_PAGE_SIZE / 2;
  const items = buildTranscript(turns);
  const scenario = opts.scenario ?? "custom";

  let historyFetchMs = 0;
  let snapshotFetchMs = 0;

  const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input.toString();
    const path = url.split("?")[0]!;
    if (url.match(/\/v1\/sessions\/[^/]+\/stream$/)) return streamResponse();
    if (url.match(/^\/v1\/sessions\/[^/]+\/items/)) {
      const t0 = performance.now();
      await delay(opts.historyDelayMs);
      historyFetchMs = performance.now() - t0;
      const parsed = new URL(url, "http://bench.local");
      const limit = Number(parsed.searchParams.get("limit") ?? String(SESSION_HISTORY_PAGE_SIZE));
      const order = parsed.searchParams.get("order") ?? "desc";
      const data = order === "desc" ? [...items].reverse().slice(0, limit).reverse() : items.slice(0, limit);
      return mockJson({
        object: "list",
        data,
        first_id: data[0]?.id ?? null,
        last_id: data.at(-1)?.id ?? null,
        has_more: items.length > limit,
      });
    }
    if (path === `/v1/sessions/${sessionId}` && (init?.method ?? "GET") === "GET") {
      const t0 = performance.now();
      await delay(opts.snapshotDelayMs);
      snapshotFetchMs = performance.now() - t0;
      return mockJson({
        id: sessionId,
        agent_id: "agent_bench",
        status: "idle",
        created_at: 0,
        items: [],
        labels: {},
        pending_elicitations: [],
        pending_inputs: [],
      });
    }
    if (url === "/v1/runners") {
      return mockJson({
        data: [{ runner_id: "runner_bench", online: true, harnesses: ["openai-agents"] }],
      });
    }
    throw new Error(`Unhandled fetch in sessionSwitchBench: ${init?.method ?? "GET"} ${url}`);
  };

  vi.stubGlobal("fetch", fetchImpl);

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  initChatStore(client);
  useChatStore.setState({
    conversationId: null,
    blocks: [],
    pendingUserMessages: [],
    pendingByConversation: {},
    activeResponse: null,
    status: "idle",
    sessionStatus: "idle",
    loadingConversation: false,
    conversationLoadError: null,
    abortController: null,
  });

  let firstBubbleMs = 0;
  let metadataLagMs = 0;
  let firstBubbleAt = 0;
  let metadataAt = 0;
  let startAt = 0;

  const unsub = useChatStore.subscribe((state) => {
    const now = performance.now();
    if (
      firstBubbleMs === 0 &&
      state.conversationId === sessionId &&
      !state.loadingConversation &&
      state.blocks.length > 0
    ) {
      firstBubbleAt = now;
      firstBubbleMs = now - startAt;
    }
    if (metadataAt === 0 && firstBubbleAt > 0 && state.boundAgentId !== null) {
      metadataAt = now;
      metadataLagMs = now - firstBubbleAt;
    }
  });

  if (opts.prefetch) {
    prefetchSessionForSwitch(client, sessionId);
    await delay(0);
  }

  const humanDelayMs =
    opts.humanDelayMs ??
    (opts.prefetch ? Number(process.env.WEB_LATENCY_HUMAN_DELAY_MS ?? "450") : 0);
  if (humanDelayMs > 0) {
    await delay(humanDelayMs);
  }

  startAt = performance.now();
  await useChatStore.getState().switchTo(sessionId);
  const totalBindMs = performance.now() - startAt;

  unsub();
  useChatStore.getState().abortController?.abort();
  vi.unstubAllGlobals();

  return {
    scenario,
    historyDelayMs: opts.historyDelayMs,
    snapshotDelayMs: opts.snapshotDelayMs,
    prefetch: opts.prefetch === true,
    firstBubbleMs,
    historyHydratedMs: firstBubbleMs,
    metadataLagMs,
    totalBindMs,
    historyFetchMs,
    snapshotFetchMs,
  };
}
