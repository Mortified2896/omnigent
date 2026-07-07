# Web UI session-switch: load flow & latency

How the web UI loads a conversation when you switch to a different session, and
where the latency comes from. File/line references are from a code trace (not a
profiler), so treat the timing intuition as directional.

## The flow

When you click a different session, the URL param changes and `ChatPage` fires
`chatStore.switchTo()` → `bindStream()`. The key structural fact: **the snapshot
and history are fetched in parallel, but the render is blocked until both
return.**

```
 URL /c/:id changes
        │
        ▼
 switchTo(id)                          web/src/store/chatStore.ts:1411
   ├─ abort old SSE stream (1417)
   ├─ clear blocks/state, show spinner (1419-1511)   ← screen goes blank here
   └─ bindStream(id)                                  chatStore.ts:2023
        │
        ├──────────────► startStreamPump(id)   (SSE, background, non-blocking)
        │                GET /v1/sessions/:id/stream
        │
        └─ await Promise.all([ ... ])   ← BLOCKS the render (2066)
             │
   ┌─────────┴──────────────────────────────┐
   ▼                                          ▼
 REQ #1  GET /sessions/:id                  REQ #2  fetchInitialHistoryWindow
   (getSessionSlim, refresh_state=true)       (sessionsApi.ts:840)
   sessions.py:14349 → _get_session_snapshot   loops GET /sessions/:id/items?limit=20&order=desc
   backend does, mostly serially:              │  up to 8 sequential pages until
     • agent-spec bundle parse (20795)         │  2 user prompts are visible
     • context-window resolve (20828)          │
     • runner skills call    (20842) ─┐        │
     • runner model-options  (20847) ─┤ HTTP   │
     • subtree usage tree-walk(20873) │ to     │
                                       │ runner │
   ┌───────────────────────────────────┴────────┘
   ▼
 both resolved
   ├─ itemsToBlocks(items)  (2137)  ← synchronous markdown parse on main thread
   ├─ merge into Zustand    (2143-2306)
   └─ React renders all blocks (no virtualization on first paint)
        │
        ▼
   conversation visible
```

## Where the time actually goes

Two things dominate, and they're independent:

**1. The snapshot request (REQ #1) is the long pole.** `_get_session_snapshot`
with `refresh_state=true` does several things that are each best-effort but add
up, and some are *serialized runner round-trips*: fetching skills
(`sessions.py:20842`), fetching model options (`:20847`), resolving the context
window (`:20828`), and a `load_session_usage` tree-walk over the conversation's
sub-agent subtree (`:20873`). This is metadata you need for the composer/header
— **not** the messages themselves — yet the message render waits on it because
of the `Promise.all`.

**2. History fetch can be up to 8 sequential HTTP calls.**
`fetchInitialHistoryWindow` (`sessionsApi.ts:840`) pages backward 20 items at a
time until it has seen 2 user prompts. For a session with long assistant turns /
many tool calls, that's several serial round-trips before anything paints.

Then `itemsToBlocks` + the first React render are synchronous on the main thread
with no virtualization, so a large window also costs main-thread time.

## Approaches to reduce it, roughly by payoff

| # | Change | Why it helps | Effort |
|---|--------|-------------|--------|
| 1 | **Don't block message render on the snapshot.** Split the `Promise.all` — render history as soon as REQ #2 returns; let snapshot (skills, usage, model options) hydrate the header/composer a beat later. | The messages are what the user is waiting to see; today they wait on runner round-trips they don't need. Likely the biggest win. | Medium (frontend only) |
| 2 | **Make the snapshot's runner calls concurrent, or drop them from the switch path.** Skills / model-options / context-window / subtree-usage run largely serially in `_get_session_snapshot`; gather them with `asyncio.gather`, and consider serving `refresh_state` lazily (return cached, refresh in background). | Collapses several serial hops into one; or removes them from the critical path. | Medium (backend) |
| 3 | **Prefetch on hover/focus in the session list.** `queryClient.prefetchQuery(["session", id])` + prefetch the first history page when a row is hovered. | Turns most switches into warm React-Query cache hits (already `staleTime` cached once visited). | Low |
| 4 | **Fetch a single "enough" history window server-side** instead of up to 8 client round-trips — one endpoint that returns the last N items *or* back to 2 user turns in one query. | Removes the serial-pagination tail on long transcripts. | Medium |
| 5 | **Virtualize the first render + defer/offload markdown parsing** (react-window; parse below-the-fold lazily). | Cuts main-thread time on large windows so first paint is fast even when the payload is big. | Medium |
| 6 | **Keep a small LRU of recent sessions "warm"** — don't fully clear `blocks` on switch if you're returning to a recently-viewed session; show cached blocks instantly while revalidating. | Eliminates the blank-spinner flash on back-and-forth navigation. | Medium |

If I were to pick one to start: **#1 (unblock messages from the snapshot)** plus
**#3 (prefetch on hover)** together would cover both the cold switch and the
common repeat-switch case, and both are frontend-only.

## Key file references

Frontend:
- `web/src/pages/ChatPage.tsx:534,598,820` — `useParams`, `useSession`, `switchTo` effect
- `web/src/store/chatStore.ts:1411-1521` — `switchTo`
- `web/src/store/chatStore.ts:2023-2314` — `bindStream` (the `Promise.all` at 2066)
- `web/src/lib/sessionsApi.ts:753-766` — `getSessionSlim`
- `web/src/lib/sessionsApi.ts:840-857` — `fetchInitialHistoryWindow`
- `web/src/lib/itemsToBlocks.ts` — item → renderable block conversion

Backend:
- `omnigent/server/routes/sessions.py:14349-14407` — `GET /sessions/{id}`
- `omnigent/server/routes/sessions.py:20632-20881+` — `_get_session_snapshot`
- `omnigent/server/routes/sessions.py:16855-16907` — `GET /sessions/{id}/items`
- `omnigent/server/routes/sessions.py:19464-19590+` — `GET /sessions/{id}/stream`
