"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const {
  createRememberedBrowserLogins,
  loginKey,
  cookieDetails,
} = require("../src/rememberedBrowserLogins");

const cookie = {
  domain: "example.com",
  hostOnly: true,
  path: "/",
  name: "__Host-login",
  value: "private-test-login",
  secure: true,
  httpOnly: true,
  sameSite: "lax",
  session: true,
};
function browserSession(initial = []) {
  const cookies = new EventEmitter();
  let current = initial;
  cookies.get = async () => current;
  cookies.set = async (details) => {
    current = [
      ...current.filter((c) => c.name !== details.name),
      { ...details, domain: new URL(details.url).hostname, hostOnly: !details.domain },
    ];
    cookies.emit("changed");
  };
  cookies.replace = (values) => {
    current = values;
    cookies.emit("changed");
  };
  return { cookies };
}
function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-login-test-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let settings = {};
  // Model the OS encryption interface; actual Keychain encryption is exercised
  // by the separate Electron restart check.
  const safeStorage = {
    isEncryptionAvailable: () => true,
    encryptString: (text) => Buffer.from(text).map((byte) => byte ^ 0xa5),
    decryptString: (bytes) =>
      Buffer.from(bytes)
        .map((byte) => byte ^ 0xa5)
        .toString(),
  };
  const options = {
    directory,
    safeStorage,
    loadSettings: () => structuredClone(settings),
    saveSettings: (next) => {
      settings = next;
    },
  };
  return { directory, options, store: createRememberedBrowserLogins(options) };
}

test("remembered cookies restore after restart with HttpOnly and host-only rules, while other conversations and servers remain isolated", async (t) => {
  const { store, options, directory } = fixture(t),
    source = browserSession([cookie]);
  await store.prepare("http://localhost:6767", "a", source);
  assert.equal(fs.readdirSync(directory).length, 0);
  await store.remember("http://localhost:6767", "a", source);
  const files = fs.readdirSync(directory);
  assert.equal(files.length, 1);
  assert.equal(
    fs.readFileSync(path.join(directory, files[0])).includes(Buffer.from(cookie.value)),
    false,
  );
  assert.equal(fs.statSync(path.join(directory, files[0])).mode & 0o777, 0o600);
  const restarted = createRememberedBrowserLogins(options),
    restored = browserSession();
  await restarted.prepare("http://127.0.0.1:6767", "browser-tab:a:one", restored);
  const result = await restored.cookies.get({});
  assert.equal(result[0].value, cookie.value);
  assert.equal(result[0].httpOnly, true);
  assert.equal(result[0].domain, "example.com");
  assert.equal(result[0].hostOnly, true);
  assert.equal(result[0].expirationDate, undefined);
  for (const [server, conversation] of [
    ["http://localhost:6767", "b"],
    ["http://localhost:6769", "a"],
  ]) {
    const isolated = browserSession();
    await restarted.prepare(server, conversation, isolated);
    assert.equal((await isolated.cookies.get({})).length, 0);
  }
  await restarted.forget("http://localhost:6767", "a");
  assert.equal(fs.readdirSync(directory).length, 0);
  await restarted.flush();
  assert.equal(fs.readdirSync(directory).length, 0);
});

test("logout updates remembered cookies and disabling saving prevents a pending write from recreating credentials", async (t) => {
  const { store, options, directory } = fixture(t),
    source = browserSession([cookie]);
  await store.remember("https://server.example", "a", source);
  source.cookies.replace([]);
  await store.flush();
  const restored = browserSession();
  await createRememberedBrowserLogins(options).prepare("https://server.example", "a", restored);
  assert.equal((await restored.cookies.get({})).length, 0);
  source.cookies.replace([cookie]);
  await store.forget("https://server.example", "a");
  await store.flush();
  assert.equal(fs.readdirSync(directory).length, 0);
});

test("expiry is respected and credentials have no plaintext fallback when OS encryption is unavailable", async (t) => {
  assert.equal(cookieDetails({ ...cookie, expirationDate: 1 }), null);
  const future = Date.now() / 1000 + 600;
  assert.equal(cookieDetails({ ...cookie, expirationDate: future }).expirationDate, future);
  assert.notEqual(
    loginKey("https://server.example/one", "a"),
    loginKey("https://server.example/two", "a"),
  );
  const { store, options, directory } = fixture(t);
  options.safeStorage.isEncryptionAvailable = () => false;
  await assert.rejects(
    store.remember("https://server.example", "a", browserSession([cookie])),
    /System encryption/,
  );
  assert.equal(fs.readdirSync(directory).length, 0);
  assert.equal(store.isRemembered("https://server.example", "a"), false);
});
