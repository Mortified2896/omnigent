# Standalone Mac operation

A Mac can run the Omnigent server, web UI, and local host independently of RTX.
Use the current customized fork, build its web UI, and keep runtime data outside
Git. A fresh instance does not need remote chat history or a remote database.

## Build from the fork

Verify repository identity, branch/base, dirty files, and the fork push target.
Fetch current refs and use an isolated worktree when the original checkout is
stale or holds unrelated work.

```sh
uv sync --extra tracing --no-dev
pnpm install --frozen-lockfile
pnpm --dir web build
```

The web build writes `omnigent/server/static/web-ui/`, served by the Python server.
The Electron desktop app is a client; using the browser does not require rebuilding
the desktop shell.

## Isolate runtime state

Set `OMNIGENT_CONFIG_HOME` and `OMNIGENT_DATA_DIR` to separate private directories.
The first contains host identity and configuration; the second contains `chat.db`,
artifacts, and process logs. Keep the actual provider credentials in the existing
CLI credential store or supported secret storage, outside repository files.

Run the server on loopback and register the Mac host against its explicit URL:

```sh
.venv/bin/omnigent server --host 127.0.0.1 --port 6767 --no-open
.venv/bin/omnigent host --server http://127.0.0.1:6767 --no-open --non-interactive
```

Pass the same isolated environment to both processes and their children. A
per-user LaunchAgent can run each command and restart it after login; use exact
executable paths and private log files. Keep the original remote-host daemon's
config and runtime separate.

Local single-user mode requires no login on loopback. If exposing the server
beyond the machine, configure authentication and the network boundary first.
Existing Codex CLI credentials can support native Codex turns; other harnesses
may require their own installation and sign-in.

Codex is the standard implicit harness when available; explicit user and project
harness selections are retained. The model picker uses the installed CLI's live
catalog. If it omits newer models, compare the executable on `PATH` with the
Codex app's bundled CLI and set `OMNIGENT_CODEX_PATH` to the intended executable
in the server/host environment. Restart the host to refresh discovery, then
verify a real turn; a catalog entry alone does not prove account entitlement.

## Local tracing and outcomes

Follow [direct OpenTelemetry tracing](../deploy/docs/opentelemetry-tracing.md).
Reuse a suitable collector only after inspecting its routing and retention. If an
existing collector belongs to another workflow, choose unused loopback ports for
a separate collector and keep its archive independent.

Human outcomes, review comments, model attribution, and eligibility are stored
in the Omnigent chat database. Trace files provide diagnostic metadata and
correlation. A laptop instance is independent while RTX is down; cloud-backed
models still need their provider connection. Services pause while the Mac sleeps
and resume when it wakes; LaunchAgents start after user login.
