"use strict";
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
