"use strict";
const { recoveryUrl } = require("../src/rememberedBrowserLogins");
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  useLocalServerPort,
  addRememberedBrowserLogins,
} = require("../build/addRememberedBrowserLogins");

test("the component update refuses unknown layouts and invalid ports", () => {
  assert.throws(
    () => addRememberedBrowserLogins("unknown desktop", "unknown registry"),
    /does not match/,
  );
  for (const port of [0, 65536, "6767", 6767.5])
    assert.throws(() => useLocalServerPort("const CANONICAL_LOCAL_PORT = 6768;", port), /Invalid/);
  assert.throws(() => useLocalServerPort("different profile", 6767), /does not match/);
});

test("local recovery retains the active or requested conversation and never imports a foreign page URL", () => {
  const server = "http://127.0.0.1:6767/",
    conversation = `${server}c/current`;
  assert.equal(recoveryUrl(server, conversation, `${server}c/older`), conversation);
  assert.equal(recoveryUrl(server, "about:blank", conversation), conversation);
  assert.equal(recoveryUrl(server, "https://foreign.example/c/other", conversation), conversation);
  assert.equal(recoveryUrl(server, "about:blank", "https://foreign.example/c/other"), server);
});
