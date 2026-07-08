// Gated runner for session-switch latency benchmarks.
//
// Usage:
//   WEB_LATENCY_BENCH=1 WEB_LATENCY_MODE=session_switch npm test -- \
//     --run src/loadtest/sessionSwitchBench.run.test.ts
//
// Compare pre (main chatStore) vs post (current) via loadtest/session_switch_compare.sh

import { describe, expect, it } from "vitest";
import {
  runSessionSwitchBench,
  summarizeRuns,
  type SessionSwitchBenchResult,
} from "./sessionSwitchBench";

const nodeProcess = (
  globalThis as unknown as {
    process?: {
      env: Record<string, string | undefined>;
      stdout: { write: (chunk: string) => void };
    };
  }
).process;
const env = nodeProcess?.env ?? {};

const BENCH_ON = env.WEB_LATENCY_BENCH === "1";
const MODE = env.WEB_LATENCY_MODE ?? "";
const RUNS = Number(env.WEB_LATENCY_RUNS ?? "20");
const SCENARIO = env.WEB_LATENCY_SCENARIO ?? "slow_snapshot";
const LABEL = env.WEB_LATENCY_LABEL ?? "run";

type ScenarioCfg = {
  scenario: string;
  historyDelayMs: number;
  snapshotDelayMs: number;
  prefetch?: boolean;
  humanDelayMs?: number;
};

const SCENARIOS: Record<string, ScenarioCfg> = {
  slow_snapshot: { scenario: "slow_snapshot", historyDelayMs: 50, snapshotDelayMs: 800 },
  slow_history: { scenario: "slow_history", historyDelayMs: 600, snapshotDelayMs: 50 },
  prefetch_warm: {
    scenario: "prefetch_warm",
    historyDelayMs: 50,
    snapshotDelayMs: 800,
    prefetch: true,
    humanDelayMs: Number(process.env.WEB_LATENCY_HUMAN_DELAY_MS ?? "450"),
  },
};

function emit(label: string, r: SessionSwitchBenchResult & { runs?: number }): void {
  nodeProcess?.stdout.write(`WEB_LATENCY_JSON ${JSON.stringify({ label, ...r })}\n`);
}

describe("session switch benchmark", () => {
  it.skipIf(!BENCH_ON || MODE !== "session_switch")(
    "measures switchTo TTFP with controlled fetch delays",
    async () => {
      const cfg = SCENARIOS[SCENARIO];
      if (!cfg) throw new Error(`Unknown WEB_LATENCY_SCENARIO: ${SCENARIO}`);

      const runs: SessionSwitchBenchResult[] = [];
      for (let i = 0; i < RUNS; i += 1) {
        runs.push(await runSessionSwitchBench(cfg));
      }
      const summary = summarizeRuns(runs);
      emit(LABEL, summary);

      if (SCENARIO === "slow_snapshot" && LABEL === "post") {
        expect(summary.historyHydratedMs).toBeLessThan(200);
        expect(summary.metadataLagMs).toBeGreaterThan(400);
      }
      if (SCENARIO === "slow_history" && LABEL === "post") {
        expect(summary.historyHydratedMs).toBeGreaterThan(500);
      }
      if (SCENARIO === "prefetch_warm" && LABEL === "post") {
        expect(summary.historyHydratedMs).toBeLessThan(150);
      }
    },
    120_000,
  );
});
