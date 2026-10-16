// Per-viewer read receipts: the server persists the shared copy and the browser
// keeps an optimistic mirror. Every list/stream update can advance that mirror;
// pending local actions stay protected until their serialized PUT is committed.

import { useEffect, useRef, useSyncExternalStore } from "react";

import { authenticatedFetch } from "@/lib/identity";

// Bumped whenever the local mirror is written, so in-tab subscribers (the
// sidebar rows, the dock badge) recompute unseen state right away — a PUT's
// network round-trip is too slow for a click to feel live, and the
// conversations poll is slower still.
const subscribers = new Set<() => void>();
let writeVersion = 0;

function notifySubscribers(): void {
  writeVersion += 1;
  for (const cb of subscribers) cb();
}

type LastSeenMap = Record<string, number>;

// The browser mirror is hydrated at load, merged with shared receipts, and
// updated optimistically before the durable outgoing queue is flushed.
let lastSeenMap: LastSeenMap = {};
const explicitlyUnread = new Set<string>();
interface ReadWrite {
  last_seen: number;
  unread: boolean;
}
const pendingWrites = new Map<string, ReadWrite>();
const syncing = new Set<string>();
const serverVersions = new Map<string, number>();
// A poll started before a successful PUT may still finish afterwards.
const acknowledgedVersions = new Map<string, number>();
let retryTimer: ReturnType<typeof setTimeout> | undefined;
let retryDelay = 1_000;

// localStorage persistence. Best-effort everywhere: storage can be
// missing (SSR), full, or blocked — the in-memory mirror always works.
const STORAGE_KEY = "omnigent.readState.v1";
// Bound growth: keep only the newest baselines; older sessions fall back
// to the seed's read-as-of-load behavior.
const STORAGE_MAX_ENTRIES = 1000;

function hydrateFromStorage(): void {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    if (!raw) return;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return;
    const lastSeen = (parsed as { lastSeen?: unknown }).lastSeen;
    if (typeof lastSeen === "object" && lastSeen !== null) {
      for (const [id, value] of Object.entries(lastSeen)) {
        if (typeof value === "number") lastSeenMap[id] = value;
      }
    }
    const versions = (parsed as { versions?: Record<string, number> }).versions;
    for (const [id, version] of Object.entries(versions ?? {})) {
      if (Number.isSafeInteger(version) && version > 0) serverVersions.set(id, version);
    }
    const pending = (parsed as { pending?: Record<string, ReadWrite> }).pending;
    for (const [id, write] of Object.entries(pending ?? {})) {
      if (Number.isFinite(write?.last_seen) && typeof write?.unread === "boolean") {
        pendingWrites.set(id, write);
      }
    }
    const unread = (parsed as { unread?: unknown }).unread;
    if (Array.isArray(unread)) {
      for (const id of unread) {
        if (typeof id === "string") explicitlyUnread.add(id);
      }
    }
  } catch {
    // Corrupt/blocked storage → start empty.
  }
}

function persistToStorage(): void {
  try {
    let entries = Object.entries(lastSeenMap);
    if (entries.length > STORAGE_MAX_ENTRIES) {
      entries = entries.sort((a, b) => b[1] - a[1]).slice(0, STORAGE_MAX_ENTRIES);
    }
    globalThis.localStorage?.setItem(
      STORAGE_KEY,
      JSON.stringify({
        lastSeen: Object.fromEntries(entries),
        unread: [...explicitlyUnread],
        versions: Object.fromEntries(
          entries.flatMap(([id]) => (serverVersions.has(id) ? [[id, serverVersions.get(id)]] : [])),
        ),
        pending: Object.fromEntries(pendingWrites),
      }),
    );
  } catch {
    // Quota/blocked storage → in-memory state still serves this tab.
  }
}

hydrateFromStorage();

// Tracks first discovery only; subsequent server receipts are merged live.
const seeded = new Set<string>();

// Until the first seed runs we don't know the server's baselines, so the
// automatic mark-seen (useMarkConversationSeen) must NOT write — a deep-link
// / reload into /c/{id} mounts ChatPage synchronously, before the list loads,
// and an early "seen" PUT would clobber an explicit unread the server is
// about to hand us. Explicit user actions still apply.
let hydrated = false;

export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

/** Queue the latest local intent before starting its per-conversation writer. */
function syncReadState(conversationId: string): void {
  const lastSeen = lastSeenMap[conversationId];
  if (lastSeen === undefined) return;
  pendingWrites.set(conversationId, {
    last_seen: lastSeen,
    unread: explicitlyUnread.has(conversationId),
  });
  persistToStorage();
  void flushReadState(conversationId);
}

/** Serialize PUTs so a slow older request cannot undo a newer read/unread action. */
async function flushReadState(conversationId: string): Promise<void> {
  if (syncing.has(conversationId)) return;
  syncing.add(conversationId);
  try {
    let write: ReadWrite | undefined;
    while ((write = pendingWrites.get(conversationId))) {
      // Each receipt must commit before sending the next action for this chat.
      // oxlint-disable-next-line no-await-in-loop
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/read-state`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(write),
          keepalive: true,
        },
      );
      if (!response.ok) throw new Error(`Read receipt failed (${response.status})`);
      const version = Number(response.headers?.get("X-Omnigent-Read-Version"));
      if (Number.isSafeInteger(version) && version > 0) {
        acknowledgedVersions.set(conversationId, version);
      }
      if (pendingWrites.get(conversationId) === write) pendingWrites.delete(conversationId);
      persistToStorage();
    }
  } catch {
    // Retain the queue and back off while the server or network recovers.
    if (retryTimer === undefined) {
      retryTimer = setTimeout(() => {
        retryTimer = undefined;
        retryPendingWrites();
      }, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30_000);
    }
  } finally {
    syncing.delete(conversationId);
    if (pendingWrites.size === 0) {
      clearTimeout(retryTimer);
      retryTimer = undefined;
      retryDelay = 1_000;
    }
  }
}

function retryPendingWrites(): void {
  for (const id of pendingWrites.keys()) void flushReadState(id);
}

/** The read-state fields the seed reads off a conversation list item. */
export interface ReadStateSeed {
  id: string;
  viewer_last_seen?: number | null;
  viewer_unread?: boolean;
  viewer_read_version?: number;
  updated_at?: number;
  labels?: Record<string, string>;
}

/** Merge fresh shared receipts while preserving optimistic, uncommitted actions. */
export function seedReadState(conversations: readonly ReadStateSeed[]): void {
  let changed = false;
  for (const conv of conversations) {
    const firstSeed = !seeded.has(conv.id);
    seeded.add(conv.id);
    if (pendingWrites.has(conv.id)) continue;
    const local = lastSeenMap[conv.id];
    const server = typeof conv.viewer_last_seen === "number" ? conv.viewer_last_seen : undefined;
    const version = conv.viewer_read_version ?? 0;
    const knownVersion = serverVersions.get(conv.id) ?? 0;
    const acknowledged = acknowledgedVersions.get(conv.id) ?? 0;
    let baseline = local;
    let unread = explicitlyUnread.has(conv.id);
    if (version > knownVersion && version >= acknowledged && server !== undefined) {
      // Revisions order intentional Mark unread actions too (which lower last_seen).
      baseline = conv.viewer_unread ? server : Math.max(local ?? server, server);
      unread = conv.viewer_unread ?? false;
      serverVersions.set(conv.id, version);
      changed = true;
    } else if (version === 0 && knownVersion === 0 && acknowledged === 0) {
      // Compatibility with older servers: a newer read can still merge live.
      baseline =
        local !== undefined && server !== undefined ? Math.max(local, server) : (local ?? server);
      if (
        conv.viewer_unread &&
        (firstSeed || (server !== undefined && (local === undefined || server >= local)))
      )
        unread = true;
      else if (!conv.viewer_unread && server !== undefined && local !== undefined && server > local)
        unread = false;
    }
    if (baseline === undefined && typeof conv.updated_at === "number") {
      baseline = conv.labels?.["omnigent.scheduled_task_id"] ? 0 : conv.updated_at;
    }
    if (baseline !== undefined && baseline !== local) {
      lastSeenMap[conv.id] = baseline;
      changed = true;
    }
    if (unread !== explicitlyUnread.has(conv.id)) {
      if (unread) explicitlyUnread.add(conv.id);
      else explicitlyUnread.delete(conv.id);
      changed = true;
    }
  }
  if (!hydrated) {
    hydrated = true;
    changed = true;
  }
  if (changed) {
    persistToStorage();
    notifySubscribers();
  }
  retryPendingWrites();
}

/**
 * Seeds the read-state mirror from the conversation list once it loads.
 * Pass `undefined` while the list query is still loading: until a real
 * (possibly empty) list arrives we must NOT seed or flip `hydrated`, or the
 * transient empty list on a deep-link/reload would release the mark-seen
 * gate before the server's `viewer_*` read-state is known — clobbering a
 * cross-device unread (the very race the gate guards).
 */
export function useSeedReadState(conversations: readonly ReadStateSeed[] | undefined): void {
  useEffect(() => {
    if (conversations === undefined) return;
    seedReadState(conversations);
  }, [conversations]);
  useEffect(() => {
    window.addEventListener("online", retryPendingWrites);
    window.addEventListener("focus", retryPendingWrites);
    document.addEventListener("visibilitychange", retryPendingWrites);
    return () => {
      window.removeEventListener("online", retryPendingWrites);
      window.removeEventListener("focus", retryPendingWrites);
      document.removeEventListener("visibilitychange", retryPendingWrites);
    };
  }, []);
}

/**
 * Test-only: reset the module-level read-state mirror so it doesn't leak
 * between tests (the mirror is intentionally module-scoped, not React state).
 * Not used in production.
 */
export function resetReadStateForTests(): void {
  lastSeenMap = {};
  explicitlyUnread.clear();
  seeded.clear();
  pendingWrites.clear();
  syncing.clear();
  serverVersions.clear();
  acknowledgedVersions.clear();
  clearTimeout(retryTimer);
  retryTimer = undefined;
  retryDelay = 1_000;
  hydrated = false;
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEY);
  } catch {
    // Storage unavailable in this test environment — nothing to clear.
  }
}

/**
 * Clears the explicit-unread override for a conversation, re-enabling
 * automatic mark-seen. Called when the user genuinely (re)opens a thread,
 * since opening it *is* reading it. Notifies subscribers and syncs the
 * cleared state to the server when it actually removed an override.
 */
export function clearUnreadOverride(conversationId: string): void {
  if (explicitlyUnread.delete(conversationId)) {
    persistToStorage();
    notifySubscribers();
    void syncReadState(conversationId);
  }
}

/**
 * True when the user explicitly marked this conversation unread (and hasn't
 * reopened it since). Callers use this to lift the *active-row* dot
 * suppression — flagging the thread you're viewing shows the dot at once. It
 * does NOT lift the running-status suppression: a working session's dot still
 * waits for the turn to finish (see the dot condition in Sidebar's
 * ConversationRow).
 */
export function isExplicitlyUnread(conversationId: string): boolean {
  return explicitlyUnread.has(conversationId);
}

// `atSeconds` lets callers anchor the baseline to a server timestamp
// (e.g. a PATCH response's `updated_at`) instead of the client's wall
// clock — used to dismiss self-initiated `updated_at` bumps like a
// rename, which would otherwise flag the conversation unseen because
// the server's new updated_at can land slightly past the client's
// nowSeconds() under clock skew.
export function markConversationSeen(conversationId: string, atSeconds?: number): void {
  // A conversation the user explicitly marked unread stays unread until they
  // reopen it (which clears the override first). This guards every caller —
  // the automatic active-view marks and the self-action anchors
  // (rename / archive / move) alike.
  if (explicitlyUnread.has(conversationId)) return;
  // Before hydrate resolves we don't have the server's baselines; writing
  // now could clobber an explicit unread we're about to load (the reload
  // race). Explicit user actions below are exempt — they reflect intent.
  if (!hydrated) return;
  const baseline = atSeconds ?? nowSeconds();
  const stored = lastSeenMap[conversationId];
  if (stored !== undefined && stored >= baseline) return;
  lastSeenMap[conversationId] = baseline;
  persistToStorage();
  notifySubscribers();
  void syncReadState(conversationId);
}

/**
 * Forces a conversation back to "unseen" — the inverse of
 * {@link markConversationSeen}, backing the kebab's "Mark as unread".
 * The dot's condition is `updated_at > stored`, so the baseline is
 * pinned just below the conversation's current `updated_at` (rather
 * than cleared — a missing entry reads as *seen*, not unseen). The
 * row's status still gates the dot: a "running" session won't surface
 * it until the turn finishes.
 *
 * Setting {@link explicitlyUnread} keeps the flag from being instantly
 * undone by the automatic mark-seen on the *active* thread (navigation
 * away, polls, focus) — so marking the conversation you're looking at
 * sticks. Both the baseline and the override are synced to the server, so
 * the flag also survives a reload and shows on the user's other devices.
 */
export function markConversationUnread(conversationId: string, updatedAt: number): void {
  explicitlyUnread.add(conversationId);
  lastSeenMap[conversationId] = updatedAt - 1;
  persistToStorage();
  notifySubscribers();
  void syncReadState(conversationId);
}

/**
 * Marks a conversation read on explicit user action — the inverse of
 * {@link markConversationUnread}, backing the row menu's and bulk bar's
 * "Mark as read". Clears the explicit-unread override first (it would
 * otherwise veto the baseline write), then anchors the baseline at or above
 * the row's `updated_at` so the dot clears even when the server clock leads
 * the client. Explicit intent, so unlike {@link markConversationSeen} it is
 * exempt from the hydration gate.
 */
export function markConversationRead(conversationId: string, updatedAt: number): void {
  const hadOverride = explicitlyUnread.delete(conversationId);
  const baseline = Math.max(nowSeconds(), updatedAt);
  const stored = lastSeenMap[conversationId];
  const advanced = stored === undefined || stored < baseline;
  if (advanced) lastSeenMap[conversationId] = baseline;
  if (!hadOverride && !advanced) return;
  persistToStorage();
  notifySubscribers();
  void syncReadState(conversationId);
}

/**
 * Subscribes the caller to read-state mirror writes and returns the current
 * write version, so a component re-renders (and recomputes
 * `isConversationUnseen`) the instant the user marks a row read/unread — not
 * on the next conversations poll.
 */
export function useUnseenTick(): number {
  return useSyncExternalStore(
    (onChange) => {
      subscribers.add(onChange);
      return () => subscribers.delete(onChange);
    },
    () => writeVersion,
    () => writeVersion,
  );
}

export function useConversationReadState(
  conversationId: string,
  updatedAt: number,
  status: string | undefined,
): { unseen: boolean; explicitlyUnread: boolean } {
  const read = () =>
    (isConversationUnseen(conversationId, updatedAt, status) ? 1 : 0) |
    (isExplicitlyUnread(conversationId) ? 2 : 0);
  const state = useSyncExternalStore(
    (onChange) => {
      subscribers.add(onChange);
      return () => subscribers.delete(onChange);
    },
    read,
    read,
  );
  return {
    unseen: (state & 1) !== 0,
    explicitlyUnread: (state & 2) !== 0,
  };
}

/**
 * A conversation is "unseen" only when (a) the agent has finished
 * a turn — status is "idle" or "failed", not "running" — and
 * (b) the conversation's updated_at exceeds the wall-clock time the
 * user last had it open. This avoids false positives from the
 * user's own message sends and in-flight processing bumps.
 */
export function isConversationUnseen(
  conversationId: string,
  updatedAt: number,
  status: string | undefined,
): boolean {
  if (status === "running" || status === undefined) return false;
  const stored = lastSeenMap[conversationId];
  if (stored === undefined) return false;
  return updatedAt > stored;
}

/** True when the app window currently has focus (SSR-safe default true). */
function windowHasFocus(): boolean {
  if (typeof document === "undefined") return true;
  if (document.visibilityState === "hidden") return false;
  return typeof document.hasFocus === "function" ? document.hasFocus() : true;
}

/**
 * Marks the active conversation as seen on mount, on every poll
 * refresh (updatedAt change keeps the stored time fresh), on the
 * window regaining focus, and on cleanup (navigation away).
 * Wall-clock time is stored so any server-side update that happened
 * while the user was viewing is captured, even if the conversations
 * poll hadn't picked it up yet.
 *
 * Every mark is gated on the window having focus: a thread open in a
 * blurred window is NOT being read, so a turn finishing there must
 * stay unseen (the dock badge counts it) until focus returns. The
 * focus listener covers the return path — refocusing while the
 * thread is open marks it seen at that moment.
 */
export function useMarkConversationSeen(
  conversationId: string | undefined,
  updatedAt: number | undefined,
): void {
  // A child effect may run before AppShell's first seed. Retry the active read
  // when hydration completes, even if id and updatedAt have not changed.
  const ready = useSyncExternalStore(
    (onChange) => {
      subscribers.add(onChange);
      return () => subscribers.delete(onChange);
    },
    () => hydrated,
    () => false,
  );
  // Opening a thread is reading it, so clear any explicit-unread
  // override before the mark-seen below runs (and runs first, so
  // markConversationSeen isn't no-op'd by a stale override). Keyed on
  // the id alone: a poll bumping `updatedAt` while the thread stays
  // open must NOT re-clear an override the user just set on it.
  //
  // The very first mount is skipped: an initial page load / reload while
  // sitting on a thread must NOT clear the hydrated explicit-unread
  // override (otherwise the dot you set silently vanishes on refresh).
  // ChatPage stays mounted across in-app /c/:id navigations, so this ref
  // only resets on a real reload — genuine reopens (the id changing while
  // mounted) still clear, matching "reopen = read".
  const isInitialMount = useRef(true);
  useEffect(() => {
    const wasInitial = isInitialMount.current;
    isInitialMount.current = false;
    if (!conversationId) return;
    if (wasInitial) return;
    clearUnreadOverride(conversationId);
  }, [conversationId]);

  useEffect(() => {
    if (!conversationId || updatedAt === undefined) return;
    const markIfFocused = () => {
      // Anchor at or above the updated_at being viewed: a server clock that
      // leads the client must not leave a just-read turn reading as unseen
      // (updated_at > client wall clock). Wall clock still wins when it's
      // ahead, capturing an update the poll hasn't picked up yet.
      if (windowHasFocus()) markConversationSeen(conversationId, Math.max(nowSeconds(), updatedAt));
    };
    markIfFocused();
    window.addEventListener("focus", markIfFocused);
    window.addEventListener("pageshow", markIfFocused);
    document.addEventListener("visibilitychange", markIfFocused);
    return () => {
      window.removeEventListener("focus", markIfFocused);
      window.removeEventListener("pageshow", markIfFocused);
      document.removeEventListener("visibilitychange", markIfFocused);
      // Navigation away normally happens via user interaction (focused);
      // an unmount in a blurred window (e.g. the session deleted from
      // another client) must not silently mark the thread read.
      markIfFocused();
    };
  }, [conversationId, updatedAt, ready]);
}
