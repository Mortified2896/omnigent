# Standalone Mac operation

A Mac can run the Omnigent server, web UI, and local host independently of RTX.
Use the current customized fork, build its web UI, and keep runtime data outside
Git. A fresh instance does not need remote chat history or a remote database.

## Current role — recovery and Mac-specific work (2026-10-21)

**The owner confirmed RTX recovery and selected RTX again for daily coding.**
RTX O1 is the default deployment target, with candidate inspection at Preview
`:2222` before promotion. Keep the managed Mac and its history for recovery and
Mac-specific work. The return to RTX does not itself prove the upgrade or
history merge is complete. Verify each separately.

RTX SSH, Komodo candidates, and RTX Preview are not prerequisites for an
explicitly requested Mac deployment. This guide continues to define that path.

The verified managed instance uses:

- URL: `http://localhost:6767` (loopback `127.0.0.1:6767`).
- Server LaunchAgent: `com.mortified2896.omnigent-mac.server`.
- Configuration: `/Users/Jo/.local/share/omnigent-mac/config`.
- Database: `/Users/Jo/.local/share/omnigent-mac/data/chat.db`.
- Immutable releases: `/Users/Jo/.local/share/omnigent-mac/releases/<full-sha>/`.

Inspect the live LaunchAgent, listener, `/health`, and `/v1/info` before each
deployment; these paths identify the managed workspace, not a new empty
instance. Build from the verified fork SHA into a new release directory and
verify its embedded SHA and assets before switching. Preserve a consistent
SQLite backup, the previous server plist, and the previous release for rollback.
Serialize server updates with the existing Mac deployment lock. For a frontend
change, switch only the server's release and verify health, build SHA, existing
projects/chats, and the rendered behavior. Preserve execution-host processes
and active runners. Keep isolated acceptance state separate from managed data.

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

## Start specialist MCPs only when needed

The Mac execution host can opt into session-local MCP activation by setting
`OMNIGENT_CODEX_MCP_STARTUP_SERVERS=omniroute` in its environment. Codex starts
the Omnigent bridge, its normal app integrations, the named user MCPs, and any
user MCP marked `required = true`. Other enabled user MCPs remain configured
but stopped until a task needs them. An empty value defers every optional user
MCP; leaving the variable unset preserves normal Codex startup behavior.

The agent receives `sys_mcp_servers` through the Omnigent bridge. It can list
available integrations and enable a specialist such as `anki`, `shadcn`,
`penpot`, or a Unity connection for the current chat. You can also ask directly,
for example, "Enable Anki for this chat." Activation uses Codex's supported
`config/mcpServer/reload` API; native MCP tools refresh on the next model step.
Enabling requests a connection and does not prove the server is healthy.
For a cold server whose tools are still missing, the agent calls `list` with
the server name to wait for startup and read its actual runtime status and
tool names before continuing. A failed underlying service remains a connection
or tool error; activation does not repair that service.

Selections survive resuming that chat and do not change other chats or
`~/.codex/config.toml`. Globally disabled MCPs remain unavailable. The tool
protects required/startup integrations and prevents launching an identical
transport twice under aliases such as `unity-mcp` and `unity_mcp`. Start the
underlying application or bridge when an integration needs it; keeping Unity
or Penpot deferred avoids irrelevant connection timeouts during ordinary chats.

This setting belongs on the execution host. Updating only the web server does
not update existing host processes. Verify a fresh chat on the updated host,
including actual activation and a native tool call. Preserve busy hosts and
their runners during rollout; use an isolated host for acceptance.

## Claude Code and Jev harness selection

For Anthropic models, install Claude Code using Omnigent's supported harness installer and sign in on
this Mac with `claude auth login --claudeai`. Confirm `claude auth status` reports
first-party OAuth before enabling Claude answers in Model advisor settings.
The host qualifies subscription choices from the installed CLI's live model
catalog and an account digest; API keys and managed gateways are not treated as
Claude subscription entitlement.

Claude Code can also use the Mac's existing Codex subscription through the local
`omnigent.harnesses.claude_native.codex_bridge` service. It exposes an authenticated
Anthropic Messages endpoint on loopback, while a separate signed-in Codex
app-server owns account authentication. Claude executes its tools and returns
their results to the same pending Codex turn. No Claude subscription is required
for this route; the underlying model remains OpenAI.

Configure a gateway provider with an Anthropic surface, a credential helper,
and an explicit `inference.harnesses.claude-native` model allowlist. Bind
`claude_codex_bridge.base_url`, `auth_command` and `models` to that exact launch
configuration. The authenticated bridge identity probes `account/read` and
`model/list`; only live ChatGPT-plan models with a supported Claude effort
qualify. Jev sees a separate Claude Code choice for the same checkpoint, and
launch checks the frozen subscription and bridge identity again.

The local bridge handles text and function-tool coding turns. It buffers each
API response through its next tool or completion boundary. Its ten-minute
inactivity timeout renews with each Codex event, so active long reasoning and
tool-call generation are not cut off by a fixed response deadline. Multimodal requests,
server-side Anthropic tools and Anthropic-specific billing are outside this
compatibility route. Keep it bound to loopback and its credential private.

Select Jev Router as the advisor and enable the desired OpenAI and Claude answer
choices. Each candidate is a concrete harness, model and reasoning effort:
OpenAI models can use native Codex or the qualified Claude Code bridge;
GLM choices use native Codex and Anthropic choices use native Claude Code.
The decision balance still controls whether your choice or Jev's recommendation
runs. Set it to 0% me / 100% advisor for automatic assignment. Existing defaults
and choice IDs remain compatible.

A new chat launches the chosen harness explicitly. Follow-ups remain within the
current native harness and may change its model or effort; start a new chat to
change harness. The native Claude launch rechecks the frozen account, model,
effort and connection before starting, so an account or gateway change blocks
the old route instead of silently using a different billing lane.

## Scheduled report folders

A folder's `scheduled_task_ids` config binds new runs of those automations to
that folder. The scheduler resolves bindings only among the task owner's
folders in the current workspace; an empty local owner resolves to `local`.
Missing or ambiguous bindings leave the run unfiled. Scheduled runs are not
automatically pinned, so a report assigned to Reports stays visible there.
Interactive chats retain their automatic creation-time pins.

## Automatic Direct recovery

A terminal transport/authentication failure in an OmniRoute-backed native Codex
chat automatically prepares the same native thread on the qualified Codex Direct
subscription connection. Manual model choices participate as well as Advisor
choices. Model and reasoning level are preserved; the transport change does not
replay the failed request or completed tool calls. Only that failed chat's
dedicated runner is stopped through the existing host control protocol, so the
old gateway app-server actually exits. Its native history directories are kept,
then the same native thread cold-resumes with Direct pinned. Active child work
blocks the reset and produces a visible recovery failure. Retry/message handling joins
the same recovery lock. A prominent persistent banner appears in both Chat and
Terminal views, including explicit failure details if Direct is unavailable.
Historical gateway errors remain inspectable after a later successful reply.

This recovery is server-owned and works with existing execution hosts supporting
explicit provider pinning on cold native resume. A busy unrelated chat/host does
not need to be restarted. A newly deployed web UI needs a reload in an already
open desktop/browser view.

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
