/**
 * Real-world session-switch latency via Playwright.
 *
 * Primary metrics come from `window.__OMNIGENT_SESSION_PERF__` (store hydration
 * milestones). DOM timings are secondary (render-heavy).
 */

import { test, expect } from "@playwright/test";
import { pickSwitchPair } from "./helpers/omnigentApi";

export interface SessionSwitchPerfResult {
  label: string;
  scenario: string;
  fromId: string;
  toId: string;
  runs: number;
  snapshotLeadMs: number;
  /** Instrumented: click → history hydrated in store. */
  historyHydratedMs: number;
  /** Instrumented: click → snapshot metadata hydrated. */
  snapshotHydratedMs: number;
  /** Instrumented: click → double-rAF paint milestone. */
  chatPaintedMs: number;
  /** DOM: click → hydrating placeholder hidden. */
  blankScreenMs: number;
  /** DOM: click → transcript shell mounted. */
  transcriptReadyMs: number;
  /** DOM: click → first message bubble visible. */
  bubbleVisibleMs: number;
  /** Network: click → last history GET finished. */
  historyFetchMs: number;
  /** Network: click → last snapshot GET finished. */
  snapshotFetchMs: number;
  /** snapshotFetchMs − historyFetchMs (positive = snapshot slower). */
  snapshotLeadObservedMs: number;
}

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx]!;
}

function summarize(
  label: string,
  scenario: string,
  runs: SessionSwitchPerfResult[],
): SessionSwitchPerfResult {
  const pick = (key: keyof SessionSwitchPerfResult): number => {
    const vals = runs.map((r) => r[key] as number).sort((a, b) => a - b);
    return percentile(vals, 50);
  };
  const first = runs[0]!;
  return {
    label,
    scenario,
    fromId: first.fromId,
    toId: first.toId,
    runs: runs.length,
    snapshotLeadMs: pick("snapshotLeadMs"),
    historyHydratedMs: pick("historyHydratedMs"),
    snapshotHydratedMs: pick("snapshotHydratedMs"),
    chatPaintedMs: pick("chatPaintedMs"),
    blankScreenMs: pick("blankScreenMs"),
    transcriptReadyMs: pick("transcriptReadyMs"),
    bubbleVisibleMs: pick("bubbleVisibleMs"),
    historyFetchMs: pick("historyFetchMs"),
    snapshotFetchMs: pick("snapshotFetchMs"),
    snapshotLeadObservedMs: pick("snapshotLeadObservedMs"),
  };
}

function emit(line: Record<string, unknown>): void {
  process.stdout.write(`SESSION_SWITCH_PERF_JSON ${JSON.stringify(line)}\n`);
}

/** Sidebar hover + reaction before click (ms). */
function humanInputDelays(): { hoverMs: number; reactionMs: number } {
  return {
    hoverMs: Number(process.env.SESSION_SWITCH_HOVER_MS ?? "350"),
    reactionMs: Number(process.env.SESSION_SWITCH_REACTION_MS ?? "180"),
  };
}

async function waitForChatReady(page: import("@playwright/test").Page): Promise<void> {
  await Promise.race([
    page.getByTestId("message-bubble").first().waitFor({ state: "visible", timeout: 90_000 }),
    page.getByTestId("chat-transcript-ready").waitFor({ state: "attached", timeout: 90_000 }),
    page.getByLabel("Message the agent").waitFor({ state: "visible", timeout: 90_000 }),
    page.getByText("What should we work on?").waitFor({ state: "visible", timeout: 90_000 }),
  ]);
}

async function readPerf(page: import("@playwright/test").Page) {
  return page.evaluate(() => window.__OMNIGENT_SESSION_PERF__ ?? null);
}

async function measureOneSwitch(
  page: import("@playwright/test").Page,
  fromId: string,
  toId: string,
  scenario: string,
): Promise<Omit<SessionSwitchPerfResult, "label" | "runs">> {
  await page.goto(`/c/${fromId}`);
  await waitForChatReady(page);

  let snapshotFetchMs = 0;
  let historyFetchMs = 0;

  const onResponse = (response: import("@playwright/test").Response): void => {
    const url = response.url();
    const method = response.request().method();
    if (method !== "GET") return;
    if (url.includes(`/sessions/${toId}/items`)) {
      historyFetchMs = Math.max(historyFetchMs, performance.now());
    } else if (
      url.includes(`/sessions/${encodeURIComponent(toId)}`) &&
      !url.includes("/items") &&
      !url.includes("/stream")
    ) {
      snapshotFetchMs = Math.max(snapshotFetchMs, performance.now());
    }
  };

  const t0 = performance.now();
  page.on("response", onResponse);

  const row = page.locator(`a[href="/c/${toId}"]`).first();
  await row.scrollIntoViewIfNeeded();
  const { hoverMs, reactionMs } = humanInputDelays();
  if (scenario === "cold_click") {
    await page.waitForTimeout(reactionMs);
  } else if (scenario === "hover_prefetch") {
    await row.hover();
    await page.waitForTimeout(150);
  } else {
    // human_click (default): hover long enough for sidebar prefetch debounce,
    // then a short reaction pause before clicking.
    await row.hover();
    await page.waitForTimeout(hoverMs);
    await page.waitForTimeout(reactionMs);
  }
  await row.click();

  const placeholder = page
    .getByTestId("hydrating-placeholder")
    .or(page.getByText("Loading conversation…"));
  await placeholder.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  const blankScreenMs = await placeholder
    .waitFor({ state: "hidden", timeout: 90_000 })
    .then(() => performance.now() - t0)
    .catch(() => 0);

  const transcriptReadyMs = await Promise.race([
    page
      .getByTestId("chat-transcript-ready")
      .waitFor({ state: "attached", timeout: 90_000 })
      .then(() => performance.now() - t0),
    page
      .getByTestId("message-bubble")
      .first()
      .waitFor({ state: "visible", timeout: 90_000 })
      .then(() => performance.now() - t0),
  ]).catch(() => 0);

  const bubbleVisibleMs = await page
    .getByTestId("message-bubble")
    .first()
    .waitFor({ state: "visible", timeout: 90_000 })
    .then(() => performance.now() - t0)
    .catch(() => transcriptReadyMs);

  // Poll until instrumentation publishes snapshot hydration or timeout.
  let perf = await readPerf(page);
  for (let i = 0; i < 200 && (perf?.snapshotHydratedMs == null || perf.snapshotHydratedMs <= 0); i += 1) {
    await page.waitForTimeout(25);
    perf = await readPerf(page);
  }

  page.off("response", onResponse);

  const relSnapshot = snapshotFetchMs > 0 ? snapshotFetchMs - t0 : 0;
  const relHistory = historyFetchMs > 0 ? historyFetchMs - t0 : 0;

  return {
    scenario,
    fromId,
    toId,
    snapshotLeadMs: 0,
    historyHydratedMs: perf?.historyHydratedMs ?? 0,
    snapshotHydratedMs: perf?.snapshotHydratedMs ?? 0,
    chatPaintedMs: perf?.chatPaintedMs ?? 0,
    blankScreenMs: blankScreenMs || transcriptReadyMs || bubbleVisibleMs,
    transcriptReadyMs: transcriptReadyMs || bubbleVisibleMs,
    bubbleVisibleMs: bubbleVisibleMs || transcriptReadyMs,
    historyFetchMs: relHistory,
    snapshotFetchMs: relSnapshot,
    snapshotLeadObservedMs: relHistory > 0 && relSnapshot > 0 ? relSnapshot - relHistory : 0,
  };
}

test.describe("session switch perf", () => {
  test("measures real switch latency against OMNIGENT_URL", async ({ page }) => {
    const runsWanted = Number(process.env.SESSION_SWITCH_RUNS ?? "5");
    const label = process.env.SESSION_SWITCH_LABEL ?? "e2e";
    const scenario = process.env.SESSION_SWITCH_SCENARIO ?? "human_click";
    const pair = await pickSwitchPair();
    process.stdout.write(
      `SESSION_SWITCH_PAIR_JSON ${JSON.stringify({ label, scenario, ...pair })}\n`,
    );

    const runs: SessionSwitchPerfResult[] = [];
    for (let i = 0; i < runsWanted; i += 1) {
      const [from, to] = i % 2 === 0 ? [pair.fromId, pair.toId] : [pair.toId, pair.fromId];
      const sample = await measureOneSwitch(page, from, to, scenario);
      const row: SessionSwitchPerfResult = {
        label,
        runs: 1,
        snapshotLeadMs: pair.snapshotLeadMs,
        ...sample,
      };
      runs.push(row);
      emit({ kind: "sample", ...row });
    }

    const summary = summarize(label, scenario, runs);
    emit({ kind: "summary", ...summary });

    const readyMs = summary.transcriptReadyMs || summary.bubbleVisibleMs;
    expect(readyMs).toBeGreaterThan(0);
    expect(readyMs).toBeLessThan(120_000);
    if (label === "post" && summary.historyHydratedMs > 0) {
      expect(summary.historyHydratedMs).toBeLessThan(summary.blankScreenMs + 500);
    }
  });
});
