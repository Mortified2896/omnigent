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
  rememberedBrowserLoginOptions,
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
  await Promise.all(
    [
      ["http://localhost:6767", "b"],
      ["http://localhost:6769", "a"],
    ].map(async ([server, conversation]) => {
      const isolated = browserSession();
      await restarted.prepare(server, conversation, isolated);
      assert.equal((await isolated.cookies.get({})).length, 0);
    }),
  );
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

test("explicit reuse seeds new chats after restart, with a single source and server/mount isolation", async (t) => {
  const { store, options, directory } = fixture(t);
  const server = "https://server.example/one";
  const source = browserSession([cookie]);
  await store.share(server, "browser-tab:a:one", source);
  const restarted = createRememberedBrowserLogins(options);
  const recipient = browserSession();
  await Promise.all([
    restarted.prepare(server, "b", recipient),
    restarted.prepare(server, "browser-tab:b:one", recipient),
  ]);
  assert.equal((await recipient.cookies.get({}))[0].value, cookie.value);
  assert.equal(restarted.isRemembered(server, "b"), false);
  recipient.cookies.replace([{ ...cookie, value: "another-account" }]);
  await restarted.flush();
  assert.equal(fs.readdirSync(directory).length, 1);
  const next = browserSession();
  await restarted.prepare(server, "c", next);
  assert.equal((await next.cookies.get({}))[0].value, cookie.value);
  await Promise.all(
    [
      "https://other.example/one",
      "https://server.example/two",
      "https://server.example:444/one",
    ].map(async (other) => {
      const isolated = browserSession();
      await restarted.prepare(other, "a", isolated);
      assert.deepEqual(await isolated.cookies.get({}), []);
      assert.equal(restarted.isShared(other), false);
    }),
  );
  // Only the selected source's updates affect the reusable saved login.
  source.cookies.replace([]);
  await store.flush();
  const afterLogout = browserSession();
  await restarted.prepare(server, "d", afterLogout);
  assert.deepEqual(await afterLogout.cookies.get({}), []);
});

test("reuse preserves an existing account and a chat's own saved cookies", async (t) => {
  const { store, options } = fixture(t);
  const server = "https://server.example";
  await store.remember(server, "personal", browserSession([{ ...cookie, value: "personal" }]));
  await store.share(server, "source", browserSession([cookie]));
  const restarted = createRememberedBrowserLogins(options);
  const personal = browserSession();
  await restarted.prepare(server, "personal", personal);
  assert.equal((await personal.cookies.get({}))[0].value, "personal");
  const live = browserSession([{ ...cookie, value: "live" }]);
  await restarted.prepare(server, "existing", live);
  assert.equal((await live.cookies.get({}))[0].value, "live");
  const recipient = browserSession();
  await restarted.prepare(server, "recipient", recipient);
  recipient.cookies.replace([]);
  await restarted.prepare(server, "browser-tab:recipient:new", recipient);
  assert.deepEqual(await recipient.cookies.get({}), []);
});

test("disabling reuse blocks new imports; forgetting its source deletes credentials and stops queued saves", async (t) => {
  const { store, options, directory } = fixture(t);
  const server = "https://server.example";
  const source = browserSession([cookie]);
  await store.share(server, "source", source);
  await store.unshare(server);
  assert.equal(store.isShared(server), false);
  assert.equal(store.isRemembered(server, "source"), true);
  const fresh = browserSession();
  await createRememberedBrowserLogins(options).prepare(server, "other", fresh);
  assert.deepEqual(await fresh.cookies.get({}), []);
  await store.share(server, "source", source);
  source.cookies.replace([{ ...cookie, value: "queued" }]);
  await store.forget(server, "browser-tab:source:one");
  await store.flush();
  assert.equal(store.isShared(server), false);
  assert.deepEqual(fs.readdirSync(directory), []);
  const afterForget = browserSession();
  await createRememberedBrowserLogins(options).prepare(server, "another", afterForget);
  assert.deepEqual(await afterForget.cookies.get({}), []);
});

test("reuse fails closed for unavailable OS encryption, basic_text, and preference write failures", async (t) => {
  const { store, options, directory } = fixture(t);
  const server = "https://server.example";
  options.safeStorage.getSelectedStorageBackend = () => "basic_text";
  await assert.rejects(store.share(server, "a", browserSession([cookie])), /System encryption/);
  assert.equal(store.isShared(server), false);
  assert.deepEqual(fs.readdirSync(directory), []);
  delete options.safeStorage.getSelectedStorageBackend;
  const broken = createRememberedBrowserLogins({
    ...options,
    saveSettings: () => {
      throw new Error("write failed");
    },
  });
  await assert.rejects(broken.share(server, "a", browserSession([cookie])), /write failed/);
  assert.equal(broken.isShared(server), false);
  assert.deepEqual(fs.readdirSync(directory), []);
});

test("the native menu enables and disables reuse through the same options used by main", async (t) => {
  const { store } = fixture(t);
  const server = "https://server.example";
  const options = rememberedBrowserLoginOptions({
    store,
    getServerUrl: () => server,
    showError: () => assert.fail("unexpected preference error"),
  });
  const entry = {
    conversationId: "a",
    view: { webContents: { session: browserSession([cookie]) } },
  };
  let menu = options.additionalContextMenuItems(entry);
  assert.equal(menu[1].checked, false);
  await menu[1].click({ checked: true });
  menu = options.additionalContextMenuItems(entry);
  assert.equal(menu[0].checked, true);
  assert.equal(menu[1].checked, true);
  const fresh = browserSession();
  await options.prepareSession("b", fresh);
  assert.equal((await fresh.cookies.get({}))[0].value, cookie.value);
  await menu[1].click({ checked: false });
  assert.equal(store.isShared(server), false);
});
