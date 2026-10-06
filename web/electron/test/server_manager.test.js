const { describe, it, mock, afterEach } = require("node:test");
const assert = require("node:assert/strict");

const cli = require("../src/omnigent_cli");
const serverManager = require("../src/server_manager");
const { ensureServerAuth } = serverManager;
const SERVER = "https://app.example.com";
const CLI_PATH = "/bin/omnigent";

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

describe("ensureServerAuth", () => {
  afterEach(() => {
    mock.restoreAll();
  });

  it("skips auth entirely for a loopback server (no probe, no login)", async () => {
    mock.method(cli, "isLoopbackServer", () => true);
    const probe = mock.method(cli, "probeServerAuth", async () => ({
      authed: false,
      reachable: true,
    }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "" }));

    const res = await ensureServerAuth(CLI_PATH, "http://localhost:6767");

    assert.deepEqual(res, { ok: true });
    assert.equal(probe.mock.callCount(), 0);
    assert.equal(login.mock.callCount(), 0);
  });

  it("skips login when the probe reports already authed", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: true, reachable: true }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "" }));
    const onLogin = mock.fn();

    const res = await ensureServerAuth(CLI_PATH, SERVER, { onLogin });

    assert.deepEqual(res, { ok: true });
    assert.equal(login.mock.callCount(), 0);
    assert.equal(onLogin.mock.callCount(), 0);
  });

  it("skips login (defers to the connect attempt) when the server is unreachable", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: false }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "" }));

    const res = await ensureServerAuth(CLI_PATH, SERVER);

    assert.deepEqual(res, { ok: true });
    assert.equal(login.mock.callCount(), 0);
  });

  it("runs login when not authed, and returns ok on success", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: true, output: "Logged in." }));
    const onLogin = mock.fn();

    const res = await ensureServerAuth(CLI_PATH, SERVER, { onLogin });

    assert.deepEqual(res, { ok: true });
    assert.equal(login.mock.callCount(), 1);
    assert.equal(onLogin.mock.callCount(), 1);
    assert.deepEqual(login.mock.calls[0].arguments, [CLI_PATH, SERVER]);
  });

  it("passes an isaac omni descriptor through login and names it in the safe error", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    const login = mock.method(cli, "loginServer", async () => ({ ok: false, output: "SECRET" }));
    const command = {
      executable: "/usr/local/bin/isaac",
      prefixArgs: ["omni"],
      displayName: "isaac omni",
    };

    const res = await ensureServerAuth(command, SERVER);

    assert.deepEqual(login.mock.calls[0].arguments, [command, SERVER]);
    assert.equal(res.authError, true);
    assert.match(res.error, /isaac omni login https:\/\/app\.example\.com/);
    assert.doesNotMatch(res.error, /SECRET/);
  });

  it("returns an authError with a generic message and does NOT surface raw login output", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    // `omnigent login` stdout on the OIDC path can carry the login-ticket URL
    // (auth material); it must never reach the renderer via the error string.
    mock.method(cli, "loginServer", async () => ({
      ok: false,
      output: "Opening browser for login: https://app.example.com/auth/login?ticket=SECRET123",
    }));

    const res = await ensureServerAuth(CLI_PATH, SERVER);

    assert.equal(res.ok, false);
    assert.equal(res.authError, true);
    assert.doesNotMatch(res.error, /ticket=|SECRET123/);
    assert.match(res.error, /omnigent login https:\/\/app\.example\.com/);
  });

  it("uses the same generic message when login fails with no output", async () => {
    mock.method(cli, "isLoopbackServer", () => false);
    mock.method(cli, "probeServerAuth", async () => ({ authed: false, reachable: true }));
    mock.method(cli, "loginServer", async () => ({ ok: false, output: "" }));

    const res = await ensureServerAuth(CLI_PATH, SERVER);

    assert.equal(res.ok, false);
    assert.equal(res.authError, true);
    assert.match(res.error, /omnigent login https:\/\/app\.example\.com/);
  });
});
