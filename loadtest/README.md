# Session-switch load tests

Benchmarks for sidebar session switching. Product instrumentation lives on
`ui-perf-improv`; scripts and Playwright harness live on `ui-perf-improv-bench`.

## Metrics

| Metric | Source | Meaning |
|--------|--------|---------|
| `historyHydratedMs` | `window.__OMNIGENT_SESSION_PERF__` | Click → history merged in store (`loadingConversation` cleared) |
| `snapshotHydratedMs` | same | Click → snapshot metadata applied |
| `chatPaintedMs` | same | Click → double-rAF after history hydrate |
| `blankScreenMs` | DOM | Click → `hydrating-placeholder` hidden |
| `transcriptReadyMs` | DOM | Click → `chat-transcript-ready` attached |
| `bubbleVisibleMs` | DOM | Click → first `message-bubble` visible |
| `historyFetchMs` / `snapshotFetchMs` | network | Click → last matching GET finished |

Prefer **instrumented** metrics for comparing the two-phase `bindStream` change;
DOM bubble timings include markdown render cost and often swamp the hydration win.

## Vitest (mocked network)

```bash
cd web
npm run loadtest:session-switch
```

Delays are controlled via env vars in `sessionSwitchBench.ts` (defaults 50ms history,
800ms snapshot). `WEB_LATENCY_HUMAN_DELAY_MS` (default 450) simulates sidebar hover
+ read time before click when prefetch is enabled.

Playwright uses `SESSION_SWITCH_HOVER_MS` (default 350) and
`SESSION_SWITCH_REACTION_MS` (default 180) for the `human_click` scenario.

## Playwright (real remote API)

Requires `web/.env.local` with `OMNIGENT_URL` and `OMNIGENT_AUTH_TOKEN`, and a
running Vite dev server (`npm run dev`).

```bash
./loadtest/session_switch_playwright.sh
```

Pre/post comparison swaps `main` `chatStore.ts` + `ChatPage.tsx` for PRE, then
restores the branch copies for POST:

```bash
./loadtest/session_switch_playwright_compare.sh
```

`pickSwitchPair()` probes candidate sessions and prefers pairs where the target
snapshot is slower than history (`SESSION_SWITCH_MIN_SNAPSHOT_LEAD_MS`, default 200).
