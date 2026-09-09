const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { createLocalServerRecovery } = require("../src/local_server_recovery");

function windowStub() {
  return { isDestroyed: () => false };
}

describe("local server recovery", () => {
  it("starts a dead loopback server and navigates to the recovered URL", async () => {
    const loaded = [];
    const remembered = [];
    let started = false;
    const recovery = createLocalServerRecovery({
      isLoopbackServer: () => true,
      sameLoopbackServer: (a, b) => new URL(a).port === new URL(b).port,
      probeServer: async () => started,
      resolveCliPath: () => "/opt/omnigent",
      startLocalServer: async (_cli, targetUrl) => {
        started = true;
        return { ok: true, url: targetUrl };
      },
      loadUrl: async (_win, url) => loaded.push(url),
      rememberUrl: (url) => remembered.push(url),
    });

    const result = await recovery.ensure(windowStub(), "http://127.0.0.1:6768", {
      reason: "launch",
    });
    assert.deepEqual(result, {
      handled: true,
      ok: true,
      url: "http://127.0.0.1:6768",
    });
    assert.deepEqual(loaded, ["http://127.0.0.1:6768"]);
    assert.deepEqual(remembered, ["http://127.0.0.1:6768"]);
  });

  it("reloads a healthy server only when renderer recovery requests it", async () => {
    const loaded = [];
    const recovery = createLocalServerRecovery({
      isLoopbackServer: () => true,
      sameLoopbackServer: (a, b) => new URL(a).port === new URL(b).port,
      probeServer: async () => true,
      resolveCliPath: () => {
        throw new Error("must not resolve the CLI for a healthy server");
      },
      startLocalServer: async () => {
        throw new Error("must not start a healthy server");
      },
      loadUrl: async (_win, url) => loaded.push(url),
    });
    const win = windowStub();

    await recovery.ensure(win, "http://localhost:6767", { reason: "watchdog" });
    assert.deepEqual(loaded, []);
    await recovery.ensure(win, "http://localhost:6767", {
      reason: "renderer-gone",
      reloadWhenHealthy: true,
    });
    assert.deepEqual(loaded, ["http://localhost:6767"]);
  });

  it("deduplicates simultaneous recovery signals across windows", async () => {
    let starts = 0;
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    const recovery = createLocalServerRecovery({
      isLoopbackServer: () => true,
      sameLoopbackServer: (a, b) => new URL(a).port === new URL(b).port,
      probeServer: async () => starts > 0,
      resolveCliPath: () => "/opt/omnigent",
      startLocalServer: async (_cli, targetUrl) => {
        starts += 1;
        await gate;
        return { ok: true, url: targetUrl };
      },
      loadUrl: async () => {},
    });
    const first = recovery.ensure(windowStub(), "http://127.0.0.1:6768");
    const second = recovery.ensure(windowStub(), "http://127.0.0.1:6768");
    release();
    await Promise.all([first, second]);
    assert.equal(starts, 1);
  });

  it("never starts processes for a remote server", async () => {
    let starts = 0;
    const recovery = createLocalServerRecovery({
      isLoopbackServer: () => false,
      sameLoopbackServer: () => false,
      probeServer: async () => false,
      resolveCliPath: () => "/opt/omnigent",
      startLocalServer: async () => {
        starts += 1;
        return { ok: true, url: "http://127.0.0.1:6767" };
      },
      loadUrl: async () => {},
    });

    const result = await recovery.ensure(windowStub(), "https://server.example");
    assert.deepEqual(result, { handled: false, ok: false });
    assert.equal(starts, 0);
  });

  it("fails closed when the CLI reports a different port", async () => {
    const loaded = [];
    let probes = 0;
    const recovery = createLocalServerRecovery({
      isLoopbackServer: () => true,
      sameLoopbackServer: (a, b) => new URL(a).port === new URL(b).port,
      probeServer: async () => {
        probes += 1;
        return false;
      },
      resolveCliPath: () => "/opt/omnigent",
      startLocalServer: async () => ({ ok: true, url: "http://127.0.0.1:6767" }),
      loadUrl: async (_win, url) => loaded.push(url),
    });

    const result = await recovery.ensure(windowStub(), "http://127.0.0.1:6768");
    assert.equal(result.ok, false);
    assert.match(result.error, /6767.*6768/);
    assert.deepEqual(loaded, []);
    assert.equal(probes, 1);
  });
});
