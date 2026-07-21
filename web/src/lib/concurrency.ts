// Bounded-concurrency worker pool.
//
// `Promise.all(items.map(work))` fires every request in the same
// microtask. For N in the hundreds (bulk delete / archive across
// hundreds of conversations) that overwhelms:
//   - the browser's per-origin connection limit (HTTP/1.1 default 6;
//     HTTP/2 stream limits vary by host),
//   - the single uvicorn worker handling our API on this host,
//   - the SQLite writer when several deletes land at once.
//
// `runWithConcurrency` processes items through a fixed number of
// parallel workers, each pulling the next pending index when it
// finishes. Order of completion is not preserved — callers that care
// should sort results back by their original index.
//
// Used by useBulkDeleteConversations / useBulkArchiveConversations to
// keep both endpoints friendly to the backend and the browser.

export interface ConcurrencyResult<T> {
  index: number;
  value: T | undefined;
  error: unknown;
}

export async function runWithConcurrency<T>(
  items: readonly T[],
  worker: (item: T, index: number) => Promise<unknown>,
  concurrency: number,
): Promise<ConcurrencyResult<unknown>[]> {
  const size = items.length;
  const results: ConcurrencyResult<unknown>[] = new Array(size);
  if (size === 0) return results;
  const limit = Math.max(1, Math.min(concurrency, size));
  let nextIndex = 0;
  async function pump(): Promise<void> {
    while (true) {
      const i = nextIndex;
      nextIndex += 1;
      if (i >= size) return;
      try {
        const value = await worker(items[i], i);
        results[i] = { index: i, value, error: undefined };
      } catch (error) {
        results[i] = { index: i, value: undefined, error };
      }
    }
  }
  const workers = Array.from({ length: limit }, () => pump());
  await Promise.all(workers);
  return results;
}