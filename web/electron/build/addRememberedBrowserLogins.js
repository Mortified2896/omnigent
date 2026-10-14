"use strict";

// Component update for the inspected 0.9 desktop build. Fail closed on an
// unknown source layout; never replace unrelated desktop behavior.
function once(source, before, after) {
  if (source.split(before).length !== 2) throw new Error("The installed desktop source does not match the inspected build.");
  return source.replace(before, after);
}

function addRememberedBrowserLogins(mainSource, registrySource) {
  let main = mainSource;
  let registry = registrySource;
  main = once(main, 'const { createBrowserViewRegistry } = require("./browserViewRegistry");', `const { createBrowserViewRegistry } = require("./browserViewRegistry");
const { safeStorage } = require("electron");
const { createRememberedBrowserLogins, rememberedBrowserLoginOptions, recoveryUrl } = require("./rememberedBrowserLogins");
const rememberedBrowserLogins = createRememberedBrowserLogins({
  directory: () => path.join(app.getPath("userData"), "remembered-browser-logins"),
  safeStorage, loadSettings, saveSettings,
  onError: () => dialog.showErrorBox("Browser login could not be saved", "Your current session stays open. Try enabling Remember logins again."),
});`);
  main = once(main, "return createBrowserViewRegistry({", `return createBrowserViewRegistry({
    ...rememberedBrowserLoginOptions({
      store: rememberedBrowserLogins,
      getServerUrl: () => windows.get(win)?.serverUrl,
      showError: (title, message) => dialog.showErrorBox(title, message),
    }),
    showContextMenu: items => Menu.buildFromTemplate(items).popup({ window: win }),`);
  main = once(main, "await serverManager.shutdown(cliPath);", "await rememberedBrowserLogins.flush().catch(() => {});\n      await serverManager.shutdown(cliPath);");
  main = once(main, "function loadServerUrl(win, serverUrl, routePath) {", "function loadServerUrl(win, serverUrl, routePath, loadUrl) {");
  main = once(main,
    "  return win.loadURL(routePath ? resolveServerPath(serverUrl, routePath) : serverUrl);",
    "  const target = loadUrl || (routePath ? resolveServerPath(serverUrl, routePath) : serverUrl);\n  const state = windows.get(win);\n  if (state) state.recoveryLoadUrl = target;\n  return win.loadURL(target);");
  main = once(main, "    serverUrl: destination ? serverUrl : null,", "    serverUrl: destination ? serverUrl : null,\n    recoveryLoadUrl: destination,");
  main = once(main,
    "  loadUrl: (win, serverUrl) => loadServerUrl(win, normalizeUrl(serverUrl)),",
    "  loadUrl: (win, serverUrl) => loadServerUrl(win, normalizeUrl(serverUrl), undefined, recoveryUrl(serverUrl, win.webContents.getURL(), windows.get(win)?.recoveryLoadUrl)),");
  registry = once(registry, "const DEFAULT_CAP = 10;", `const DEFAULT_CAP = 10;
const { owningConversation } = require("./rememberedBrowserLogins");
let registrySeq = 0;`);
  registry = once(registry, "  getHostDisplayScaleFactor = () => null,", `  getHostDisplayScaleFactor = () => null,
  additionalContextMenuItems = () => [],
  showContextMenu = () => {},
  prepareSession = null,
  partitionScope = "w" + (++registrySeq),`);
  registry = once(registry, '      lastRequestedUrl: "",', '      lastRequestedUrl: "",\n      sessionReady: null,');
  registry = once(registry, "        nodeIntegration: false,", '        partition: "omnigent-agent-" + partitionScope + "-" + owningConversation(conversationId),\n        nodeIntegration: false,');
  registry = once(registry, "    entries.set(conversationId, entry);", `    entries.set(conversationId, entry);
    view.webContents.on("context-menu", () => {
      const items = additionalContextMenuItems(entry);
      if (items.length) showContextMenu(items);
    });`);
  registry = once(registry, "          entry.view.webContents.loadURL(url);", `          if (prepareSession) {
            entry.sessionReady ??= Promise.resolve().then(() => prepareSession(conversationId, entry.view.webContents.session));
            void entry.sessionReady.then(() => {
              if (get(conversationId) === entry && entry.lastRequestedUrl === url) return entry.view.webContents.loadURL(url);
            }).catch(() => sendToRenderer("browser-nav-blocked", { conversationId, url, error: "The browser could not open this page." }));
          } else entry.view.webContents.loadURL(url);`);
  return { main, registry };
}

function useLocalServerPort(profile, port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid local server port.");
  return once(profile, "const CANONICAL_LOCAL_PORT = 6768;", `const CANONICAL_LOCAL_PORT = ${port};`);
}

module.exports = { addRememberedBrowserLogins, useLocalServerPort };
