// Self-healing coordinator for a desktop window backed by a loopback server.

"use strict";

/**
 * Build a serialized local-server recovery function. Dependencies are injected
 * so the retry policy can be tested without Electron or real processes.
 *
 * @param {{
 *   isLoopbackServer: (url: string) => boolean,
 *   sameLoopbackServer: (a: string, b: string) => boolean,
 *   probeServer: (url: string) => Promise<boolean>,
 *   resolveCliPath: () => string | null,
 *   startLocalServer: (cliPath: string, targetUrl: string) => Promise<{ok: boolean, url?: string, error?: string}>,
 *   loadUrl: (win: object, url: string) => Promise<unknown>,
 *   rememberUrl?: (url: string) => void,
 *   log?: {info?: Function, warn?: Function},
 * }} deps
 */
function createLocalServerRecovery(deps) {
  const inFlight = new WeakMap();
  const targetStarts = new Map();

  async function ensureTarget(targetUrl, reason) {
    const key = targetUrl.replace(/\/+$/, "");
    const existing = targetStarts.get(key);
    if (existing) return existing;
    const operation = (async () => {
      const cliPath = deps.resolveCliPath();
      if (!cliPath) {
        return { handled: true, ok: false, error: "The omnigent CLI was not found." };
      }

      deps.log?.info?.(`[omnigent] local recovery (${reason}): starting ${targetUrl}`);
      const started = await deps.startLocalServer(cliPath, targetUrl);
      if (!started.ok || !started.url) {
        const error = started.error || "failed to start the local server";
        deps.log?.warn?.(`[omnigent] local recovery (${reason}) failed: ${error}`);
        return { handled: true, ok: false, error };
      }
      if (!deps.sameLoopbackServer(started.url, targetUrl)) {
        return {
          handled: true,
          ok: false,
          error: `Local recovery started ${started.url}, not the required ${targetUrl}.`,
        };
      }
      if (!(await deps.probeServer(targetUrl))) {
        return {
          handled: true,
          ok: false,
          error: `The required local server did not become healthy at ${targetUrl}.`,
        };
      }
      return { handled: true, ok: true, url: targetUrl };
    })().finally(() => {
      if (targetStarts.get(key) === operation) targetStarts.delete(key);
    });
    targetStarts.set(key, operation);
    return operation;
  }

  /**
   * Ensure a loopback-backed window has a live server and usable renderer.
   * Concurrent signals (load failure, focus, watchdog, renderer crash) share
   * one attempt per window so they cannot spawn competing servers.
   *
   * @param {object} win Electron BrowserWindow (kept generic for tests).
   * @param {string} targetUrl The local server URL assigned to the window.
   * @param {{reason?: string, reloadWhenHealthy?: boolean}} [opts]
   * @returns {Promise<{handled: boolean, ok: boolean, url?: string, error?: string}>}
   */
  function ensure(win, targetUrl, opts = {}) {
    if (!win || typeof targetUrl !== "string" || !deps.isLoopbackServer(targetUrl)) {
      return Promise.resolve({ handled: false, ok: false });
    }
    if (typeof win.isDestroyed === "function" && win.isDestroyed()) {
      return Promise.resolve({ handled: true, ok: false, error: "window destroyed" });
    }

    const existing = inFlight.get(win);
    if (existing) return existing;

    const reason = opts.reason || "health-check";
    const operation = (async () => {
      const wasHealthy = await deps.probeServer(targetUrl);
      const result = wasHealthy
        ? { handled: true, ok: true, url: targetUrl }
        : await ensureTarget(targetUrl, reason);
      if (!result.ok) return result;
      if (!wasHealthy || opts.reloadWhenHealthy) {
        deps.rememberUrl?.(targetUrl);
        await deps.loadUrl(win, targetUrl);
      }
      return result;
    })()
      .catch((error) => ({
        handled: true,
        ok: false,
        error: error && error.message ? error.message : String(error),
      }))
      .finally(() => {
        if (inFlight.get(win) === operation) inFlight.delete(win);
      });

    inFlight.set(win, operation);
    return operation;
  }

  return {
    ensure,
    isRecovering: (win) => inFlight.has(win),
  };
}

module.exports = { createLocalServerRecovery };
