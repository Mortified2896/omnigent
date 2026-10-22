"use strict";
// Real Electron + OS safeStorage restart check; no Google credentials,
// production cookies, Omnigent server, or paid provider are used.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const assert = require("node:assert/strict");

if (!process.versions.electron) {
  const { spawn } = require("node:child_process");
  const electron = require("electron");
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-login-electron-"));
  async function run(phase) {
    const environment = { ...process.env };
    delete environment.ELECTRON_RUN_AS_NODE;
    const flags = process.env.OMNIGENT_BROWSER_TEST_NO_SANDBOX === "1" ? ["--no-sandbox"] : [];
    const child = spawn(electron, [__filename, directory, phase, ...flags], {
      env: environment,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "",
      errors = "";
    child.stdout.on("data", (data) => {
      output += data;
    });
    child.stderr.on("data", (data) => {
      errors += data;
    });
    const timer = setTimeout(() => child.kill("SIGTERM"), 20000);
    const code = await new Promise((resolve) => {
      child.on("exit", resolve);
    });
    clearTimeout(timer);
    assert.equal(code, 0, errors.slice(-1800));
    const result = output.split("\n").find((line) => line.startsWith("LOGIN_CHECK "));
    assert.ok(result, "Electron did not report a verification result.");
    console.log(result);
  }
  (async () => {
    try {
      await run("save");
      await run("restore");
      await run("share");
      await run("restore-shared");
    } finally {
      fs.rmSync(directory, { recursive: true, force: true });
    }
  })().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
} else {
  const { app, BrowserWindow, WebContentsView, safeStorage } = require("electron");
  const http = require("node:http");
  const source = process.env.OMNIGENT_BROWSER_TEST_SOURCE || path.join(__dirname, "..", "src");
  const { createRememberedBrowserLogins, rememberedBrowserLoginOptions } = require(
    path.join(source, "rememberedBrowserLogins"),
  );
  const { createBrowserViewRegistry } = require(path.join(source, "browserViewRegistry"));
  const { createBrowserViewBoundsController } = require(path.join(source, "browserViewBounds"));
  const [directory, phase] = process.argv.slice(2);
  app.setName("Omnigent Login Verification");
  app.setPath("userData", path.join(directory, "profile"));
  app.disableHardwareAcceleration();
  app
    .whenReady()
    .then(async () => {
      const server = http.createServer((request, response) => {
        response.setHeader("Content-Type", "text/html");
        response.end(
          request.headers.cookie?.includes("alibi_login_fixture=isolated-dummy-login")
            ? "Signed in"
            : "Signed out",
        );
      });
      await new Promise((resolve) => {
        server.listen(0, "127.0.0.1", resolve);
      });
      const url = `http://127.0.0.1:${server.address().port}/`;
      const settingsFile = path.join(directory, "settings.json");
      const store = createRememberedBrowserLogins({
        directory: path.join(directory, "saved-logins"),
        safeStorage,
        loadSettings: () =>
          fs.existsSync(settingsFile) ? JSON.parse(fs.readFileSync(settingsFile, "utf8")) : {},
        saveSettings: (settings) =>
          fs.writeFileSync(settingsFile, JSON.stringify(settings), { mode: 0o600 }),
      });
      const win = new BrowserWindow({ show: false });
      let menu;
      const registry = createBrowserViewRegistry({
        WebContentsViewCtor: (options) => new WebContentsView(options),
        createBoundsController: createBrowserViewBoundsController,
        attachToHost: (view) => win.contentView.addChildView(view),
        detachFromHost: (view) => win.contentView.removeChildView(view),
        sendToRenderer: () => {},
        ...rememberedBrowserLoginOptions({
          store,
          getServerUrl: () => "https://fixture-server.example",
          showError: (title) => {
            throw new Error(title);
          },
        }),
        showContextMenu: (items) => {
          menu = items;
        },
      });
      async function open(viewId) {
        const { entry } = registry.getOrCreate(viewId);
        const loaded = new Promise((resolve) => {
          entry.view.webContents.once("did-finish-load", resolve);
        });
        registry.openOrNavigate(viewId, url);
        await loaded;
        return entry.view.webContents;
      }
      assert.equal(
        safeStorage.isEncryptionAvailable(),
        true,
        "OS encryption must be available; never fall back to plaintext.",
      );
      const browser = await open("remembered-conversation");
      if (phase === "save" || phase === "share") {
        await browser.session.cookies.set({
          url,
          name: "alibi_login_fixture",
          value: "isolated-dummy-login",
          httpOnly: true,
          sameSite: "lax",
        });
        browser.emit("context-menu", {}, { linkURL: "", selectionText: "" });
        const item = phase === "share" ? menu[1] : menu[0];
        assert.equal(item.checked, false);
        await item.click({ checked: true });
        await store.flush();
        const saved = fs.readFileSync(
          path.join(
            directory,
            "saved-logins",
            fs.readdirSync(path.join(directory, "saved-logins"))[0],
          ),
        );
        assert.equal(saved.includes(Buffer.from("isolated-dummy-login")), false);
        console.log(
          `LOGIN_CHECK ${JSON.stringify({ phase, osEncryption: true, plaintextAbsent: true })}`,
        );
      } else {
        assert.equal(await browser.executeJavaScript("document.body.innerText"), "Signed in");
        const isolated = await open("other-conversation");
        assert.equal(
          await isolated.executeJavaScript("document.body.innerText"),
          phase === "restore-shared" ? "Signed in" : "Signed out",
        );
        if (phase === "restore-shared") {
          // Recipient logouts must not overwrite the reusable source snapshot.
          await isolated.session.cookies.remove(url, "alibi_login_fixture");
          await store.flush();
          const next = await open("next-conversation");
          assert.equal(await next.executeJavaScript("document.body.innerText"), "Signed in");
          browser.emit("context-menu", {}, { linkURL: "", selectionText: "" });
          assert.equal(menu[1].checked, true);
          await menu[1].click({ checked: false });
          const afterDisable = await open("after-disable");
          assert.equal(
            await afterDisable.executeJavaScript("document.body.innerText"),
            "Signed out",
          );
        }
        browser.emit("context-menu", {}, { linkURL: "", selectionText: "" });
        assert.equal(menu[0].checked, true);
        await menu[0].click({ checked: false });
        await store.flush();
        assert.equal(fs.readdirSync(path.join(directory, "saved-logins")).length, 0);
        console.log(
          `LOGIN_CHECK ${JSON.stringify({
            phase,
            restartRestored: true,
            firstRequestAuthenticated: true,
            otherConversationSignedIn: phase === "restore-shared",
            forgetRemovedCredentials: true,
          })}`,
        );
      }
      registry.closeAll("verification-finished");
      win.destroy();
      server.close();
      app.exit(0);
    })
    .catch((error) => {
      console.error(error);
      app.exit(1);
    });
}
