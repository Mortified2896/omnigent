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

## Claude Code and Jev harness selection

Install Claude Code using Omnigent's supported harness installer and sign in on
this Mac with `claude auth login --claudeai`. Confirm `claude auth status` reports
first-party OAuth before enabling Claude answers in Model advisor settings.
The host qualifies subscription choices from the installed CLI's live model
catalog and an account digest; API keys and managed gateways are not treated as
Claude subscription entitlement.

Select Jev Router as the advisor and enable the desired OpenAI and Claude answer
choices. Each candidate is a concrete harness, model and reasoning effort:
OpenAI and GLM choices use native Codex; Claude choices use native Claude Code.
The decision balance still controls whether your choice or Jev's recommendation
runs. Set it to 0% me / 100% advisor for automatic assignment. Existing defaults
and choice IDs remain compatible.

A new chat launches the chosen harness explicitly. Follow-ups remain within the
current native harness and may change its model or effort; start a new chat to
change harness. The native Claude launch rechecks the frozen account, model,
effort and connection before starting, so an account or gateway change blocks
the old route instead of silently using a different billing lane.

## Local tracing and outcomes

Follow [direct OpenTelemetry tracing](../deploy/docs/opentelemetry-tracing.md).
Reuse a suitable collector only after inspecting its routing and retention. If an
existing collector belongs to another workflow, choose unused loopback ports for
a separate collector and keep its archive independent.

Native Codex exports completed-call usage from `tokenUsage.last`, with duplicate
notifications suppressed. Native Claude exports every API message's transcript
usage, including several messages read in one poll, and suppresses repeated
content blocks by message ID. Usage spans identify the Omnigent session, native
harness and observed model; cache counts are included only when reported.
Claude's OpenTelemetry input total includes cache reads and writes, while its
native usage gauge retains the original Anthropic counter breakdown.
Codex turn IDs are not fabricated as provider response IDs. These are Omnigent
spans, independent of vendor-native OpenTelemetry logs or beta traces.

Keep `OMNIGENT_OTEL_CAPTURE_CONTENT=false` for diagnostic metadata without
prompt, response or tool-result content. Verify a real turn in each authenticated
harness and inspect the collector's received spans; unit tests, a healthy
collector and a model catalog are separate evidence. Do not restart a busy host:
its shutdown stops its runners. Use an isolated candidate host for acceptance,
then switch the normal host once its sessions are idle.

Human outcomes, review comments, model attribution, and eligibility are stored
in the Omnigent chat database. Trace files provide diagnostic metadata and
correlation. A laptop instance is independent while RTX is down; cloud-backed
models still need their provider connection. Services pause while the Mac sleeps
and resume when it wakes; LaunchAgents start after user login.
