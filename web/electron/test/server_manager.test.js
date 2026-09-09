const { describe, it, afterEach } = require("node:test");
const assert = require("node:assert/strict");

const cli = require("../src/omnigent_cli");
const serverManager = require("../src/server_manager");

const originals = {
  localServerHealthy: cli.localServerHealthy,
  stopLocalServer: cli.stopLocalServer,
  startLocalServer: cli.startLocalServer,
};

afterEach(() => {
  Object.assign(cli, originals);
});

describe("desktop local server manager", () => {
  it("stops a managed wrong-port server before starting exact port 6768", async () => {
    const calls = [];
    cli.localServerHealthy = async () => ({ url: "http://127.0.0.1:6767" });
    cli.stopLocalServer = async () => {
      calls.push("stop");
      return { ok: true };
    };
    cli.startLocalServer = async (_path, port) => {
      calls.push(["start", port]);
      return { ok: true, url: "http://127.0.0.1:6768", port, pid: 42 };
    };

    const result = await serverManager.startLocalServer("/opt/omnigent", "http://127.0.0.1:6768/");
    assert.deepEqual(calls, ["stop", ["start", 6768]]);
    assert.equal(result.ok, true);
    assert.equal(result.url, "http://127.0.0.1:6768/");
  });

  it("fails closed when the CLI returns a different port", async () => {
    cli.localServerHealthy = async () => null;
    cli.startLocalServer = async () => ({ ok: true, url: "http://127.0.0.1:6767" });

    const result = await serverManager.startLocalServer("/opt/omnigent", "http://127.0.0.1:6768/");
    assert.equal(result.ok, false);
    assert.match(result.error, /6767.*6768/);
  });

  it("reuses only the already-running canonical server", async () => {
    let starts = 0;
    cli.localServerHealthy = async () => ({ url: "http://localhost:6768" });
    cli.startLocalServer = async () => {
      starts += 1;
      return { ok: false };
    };

    const result = await serverManager.startLocalServer("/opt/omnigent", "http://127.0.0.1:6768/");
    assert.equal(result.ok, true);
    assert.equal(result.alreadyRunning, true);
    assert.equal(starts, 0);
  });
});
