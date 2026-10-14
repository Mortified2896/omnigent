"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

function owningConversation(viewId) {
  const tab = /^browser-tab:([^:]+):[^:]+$/.exec(viewId);
  if (!tab) return viewId;
  try {
    return decodeURIComponent(tab[1]);
  } catch {
    return viewId;
  }
}

// Stable across app restarts, separated by server/mount and conversation.
function loginKey(serverUrl, viewId) {
  const url = new URL(serverUrl);
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("Invalid browser server.");
  if (["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) url.hostname = "localhost";
  const server = url.origin + url.pathname.replace(/\/+$/, "");
  return createHash("sha256")
    .update(JSON.stringify([server, owningConversation(viewId)]))
    .digest("hex");
}

function cookieDetails(cookie, now = Date.now() / 1000) {
  if (
    !cookie ||
    typeof cookie.domain !== "string" ||
    typeof cookie.name !== "string" ||
    typeof cookie.value !== "string"
  )
    return null;
  if (Number.isFinite(cookie.expirationDate) && cookie.expirationDate <= now) return null;
  const host = cookie.domain.replace(/^\./, "");
  const details = {
    url: `${cookie.secure ? "https" : "http"}://${host}${cookie.path || "/"}`,
    name: cookie.name,
    value: cookie.value,
    path: cookie.path || "/",
    secure: !!cookie.secure,
    httpOnly: !!cookie.httpOnly,
  };
  if (cookie.hostOnly === false) details.domain = cookie.domain;
  if (Number.isFinite(cookie.expirationDate)) details.expirationDate = cookie.expirationDate;
  if (["unspecified", "no_restriction", "lax", "strict"].includes(cookie.sameSite))
    details.sameSite = cookie.sameSite;
  return details;
}

// Only explicit user consent enables this store. Cookies never pass
// through a renderer, agent tool, server, plaintext file, or log.
function createRememberedBrowserLogins({
  directory,
  safeStorage,
  loadSettings,
  saveSettings,
  onError = () => {},
}) {
  const records = new Set(),
    sessions = new WeakMap();
  const root = () => (typeof directory === "function" ? directory() : directory);
  const enabled = (key) => loadSettings().remembered_browser_logins?.[key] === true;
  const file = (key) => path.join(root(), `${key}.bin`);
  function requireEncryption() {
    if (!safeStorage.isEncryptionAvailable())
      throw new Error("System encryption is unavailable. Browser logins were not saved.");
  }
  function write(key, cookies) {
    requireEncryption();
    const data = safeStorage.encryptString(JSON.stringify({ version: 1, cookies }));
    fs.mkdirSync(root(), { recursive: true, mode: 0o700 });
    const destination = file(key),
      temporary = `${destination}.tmp`;
    fs.writeFileSync(temporary, data, { mode: 0o600 });
    fs.renameSync(temporary, destination);
  }
  function attach(key, session) {
    let record = sessions.get(session);
    if (record) return record;
    record = {
      key,
      session,
      timer: null,
      pending: Promise.resolve(),
      ready: Promise.resolve(),
      active: true,
    };
    sessions.set(session, record);
    records.add(record);
    session.cookies.on("changed", () => {
      if (!record.active || !enabled(key)) return;
      clearTimeout(record.timer);
      record.timer = setTimeout(() => {
        record.timer = null;
        void capture(record).catch(() => onError());
      }, 100);
    });
    return record;
  }
  function capture(record) {
    record.pending = record.pending
      .catch(() => {})
      .then(async () => {
        if (!record.active || !enabled(record.key)) return;
        const cookies = await record.session.cookies.get({});
        if (enabled(record.key)) write(record.key, cookies);
      });
    return record.pending;
  }
  async function prepare(serverUrl, viewId, session) {
    const key = loginKey(serverUrl, viewId);
    if (!enabled(key)) return;
    const existing = sessions.get(session);
    if (existing) return existing.ready;
    const record = attach(key, session);
    record.active = false;
    record.ready = (async () => {
      requireEncryption();
      if (!fs.existsSync(file(key))) {
        record.active = true;
        return;
      }
      const bytes = fs.readFileSync(file(key));
      if (bytes.length > 2000000) throw new Error("Saved browser login is invalid.");
      const data = JSON.parse(safeStorage.decryptString(bytes));
      if (data.version !== 1 || !Array.isArray(data.cookies))
        throw new Error("Saved browser login is invalid.");
      for (const cookie of data.cookies) {
        if (!enabled(key)) break;
        const details = cookieDetails(cookie);
        if (details) await session.cookies.set(details);
      }
      record.active = true;
    })();
    return record.ready;
  }
  async function remember(serverUrl, viewId, session) {
    const key = loginKey(serverUrl, viewId);
    const cookies = await session.cookies.get({});
    write(key, cookies);
    const settings = loadSettings();
    settings.remembered_browser_logins = { ...settings.remembered_browser_logins, [key]: true };
    try {
      saveSettings(settings);
    } catch (error) {
      fs.rmSync(file(key), { force: true });
      throw error;
    }
    const record = attach(key, session);
    record.active = true;
    record.ready = Promise.resolve();
  }
  async function forget(serverUrl, viewId) {
    const key = loginKey(serverUrl, viewId),
      settings = loadSettings();
    delete settings.remembered_browser_logins?.[key];
    saveSettings(settings);
    for (const record of records)
      if (record.key === key) {
        clearTimeout(record.timer);
        record.timer = null;
        await record.pending.catch(() => {});
      }
    fs.rmSync(file(key), { force: true });
  }
  async function flush() {
    await Promise.all(
      [...records].map((record) => {
        clearTimeout(record.timer);
        record.timer = null;
        return record.ready.then(() => capture(record));
      }),
    );
  }
  return {
    prepare,
    remember,
    forget,
    flush,
    isRemembered: (serverUrl, viewId) => enabled(loginKey(serverUrl, viewId)),
  };
}

function rememberedBrowserLoginOptions({ store, getServerUrl, showError }) {
  return {
    prepareSession: async (viewId, browserSession) => {
      const serverUrl = getServerUrl();
      if (!serverUrl) return;
      try {
        await store.prepare(serverUrl, viewId, browserSession);
      } catch {
        showError(
          "Saved browser login could not be restored",
          "The browser will open signed out. Sign in again, then enable Remember logins to replace the saved session.",
        );
      }
    },
    additionalContextMenuItems: (entry) => {
      const serverUrl = getServerUrl();
      if (!serverUrl) return [];
      return [
        {
          label: "Remember logins after restarting Omnigent",
          type: "checkbox",
          checked: store.isRemembered(serverUrl, entry.conversationId),
          click: async (item) => {
            try {
              if (item.checked)
                await store.remember(
                  serverUrl,
                  entry.conversationId,
                  entry.view.webContents.session,
                );
              else await store.forget(serverUrl, entry.conversationId);
            } catch {
              showError(
                "Browser login preference was not saved",
                "Your current session stays open. The saved-login setting could not be changed.",
              );
            }
          },
        },
      ];
    },
  };
}

function recoveryUrl(serverUrl, currentUrl, requestedUrl) {
  const origin = new URL(serverUrl).origin;
  for (const candidate of [currentUrl, requestedUrl]) {
    try {
      if (new URL(candidate).origin === origin) return candidate;
    } catch {
      /* A blank window has no HTTP origin yet. */
    }
  }
  return serverUrl;
}

module.exports = {
  createRememberedBrowserLogins,
  loginKey,
  cookieDetails,
  owningConversation,
  rememberedBrowserLoginOptions,
  recoveryUrl,
};
