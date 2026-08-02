# Minimal architecture for `rebuild/upstream-0.7`

The target is to keep stock Omnigent v0.7.0's `omnigent/` core **byte-for-byte
upstream**, move every deployment-specific concern outside it, and express
the Control-Room integration (OmniRoute, Verity/Polly, Langfuse, Pi/OpenCode
Git flow, deployment, acceptance tests) via configuration, agent YAML,
plugins, environment variables, wrappers, and OpenTelemetry instrumentation
**only**.

## 1. Layers

```
rebuild/upstream-0.7
├── omnigent/                  [STOCK v0.7.0, no edits]
├── web/                       [STOCK v0.7.0, no edits]
├── sdks/                      [STOCK v0.7.0, no edits]
├── examples/polly/            [STOCK v0.7.0; we extend it for Verity/Polly]
├── deploy/                    [STOCK v0.7.0 templates only]
├── deploy/control-room/       [NEW — Control-Room integration layer]
│   ├── omniroute/
│   ├── langfuse/
│   ├── harness/
│   │   ├── pi/git.md
│   │   ├── opencode/git.md
│   │   └── worktree-conventions.md
│   ├── observability/
│   ├── acceptance/
│   └── tests/
├── scripts/control-room/      [NEW — promote/rollback/canary/updater scripts]
└── docs/control-room/         [NEW — operator & developer docs]
```

## 2. What stays upstream and what moves

### 2.1 Stays upstream (no edits)

| Concern | Upstream surface | Why it stays |
| --- | --- | --- |
| Provider catalog & OpenAI/Anthropic adapters | `omnigent/llms/`, `omnigent/llms/adapters/openai.py`, `anthropic.py`, `databricks.py` | Sufficient as a transport for any OpenAI/Anthropic-compatible gateway including OmniRoute. |
| Smart routing judge | `omnigent/server/smart_routing.py` `LLMRoutingClient` | Configured `llm:` block + `routing:` block already wires a real judge; per upstream PR #3215 the `OMNIGENT_SMART_ROUTING` env gate is dropped, so it activates from config alone. |
| External `routes:select` | `omnigent/server/smart_routing.py` `ExternalRoutingClient` (the v0.7.0 `routing-mvp` branch) | OmniRoute is an OpenAI-compatible endpoint; this client speaks the v1 proto and is pluggable via the `routing:` block. |
| Harness registry | `omnigent/harness_plugins.py` (claude-sdk, codex, pi, openai-agents, native counterparts, hermes, cursor, antigravity, goose, kiro, kimi, qwen) | All six target harnesses (Pi, OpenCode, Codex, Claude Code, Cursor, Hermes) are first-class. |
| OpenCode native | `omnigent/opencode_native*`, `omnigent/inner/opencode_native_*`, `omnigent/opencode_http_transport.py` | Production-quality harness. |
| Worktree lifecycle | `omnigent/host/git_worktree.py`, `omnigent/server/routes/_host_worktree.py`, `omnigent/server/routes/hosts.py` | Server asks the host to `git worktree add`; the host runs the actual git. The worker's worktree is the natural per-delegation unit. |
| OpenTelemetry | `omnigent/runtime/telemetry.py` | Master opt-in via `OMNIGENT_TELEMETRY_ENABLED`; OTLP exporter; `session.id` session-id span processor; redact-on-`x-api-key` payload filter; FastAPI / httpx instrumentors. Already Langfuse-compatible (Langfuse accepts OTLP at `https://cloud.langfuse.com/api/public/otel`). |
| Native watchdog | `omnigent/runtime/harnesses/_scaffold.py` `_TURN_IDLE_TIMEOUT_S` + `process_manager.py` `_resolve_harness_idle_timeout_s` | Master opt-in via `HARNESS_TURN_TIMEOUT_S` and `OMNIGENT_HARNESS_IDLE_TIMEOUT_S`. |
| Upgrade pipeline | `omni upgrade` (`omnigent/cli.py`) + `docs/omni-upgrade-design.md` | Drains, stops the local server, runs the installer's upgrade command, lazy-respawns on next call. |
| Release pipeline | `.github/workflows/release.yml`, `finalize-release.yml`, `update-homebrew.yml`, `bump-version.yml`, `release-omnigent.yml` | Stock rc→final automation. |
| PyPI publishing | `databricks/secure-public-registry-releases-eng` (cross-account) | Out of scope for the rebuild branch. |

### 2.2 Moved to Control-Room layer

| Concern | From | To | Form |
| --- | --- | --- | --- |
| OmniRoute provider config | (was: `omnigent/llms/routing.py` fork-specific `omniroute`/`minimax` provider; also `opencode_provenance_proxy.py`) | `deploy/control-room/omniroute/` | (1) An `omniroute` provider entry in the user-level `~/.omnigent/config.yaml` `providers:` block — OmniRoute is OpenAI-compatible, so `OpenAICompatibleAdapter` handles it unmodified. (2) An `OpencodeProvenanceProxy` wrapper re-implemented as a small ASGI middleware that the host process can mount in front of the OpenCode bridge. (3) An allow-listed `x-omniroute-*` header parser (the existing fork's `structured_runtime_provenance` function) reused for the Langfuse span attributes. |
| Verity orchestrator | `examples/control-room-polly/agents/opencode/config.yaml` (and the rest of the polly bundle) | `examples/polly/agents/*/config.yaml` | Reuse stock `examples/polly/agents/{verity,opencode,pi,claude_code,codex,cursor,hermes}.yaml` verbatim. (We do **not** introduce a "verity" rename — the upstream harness is still `claude-sdk` for the orchestrator.) |
| Langfuse tracing | (was: a side effect of the custom `opencode_provenance_proxy.py` and `routing_agent.py` work) | `deploy/control-room/langfuse/` | (1) Set `OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel` + `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(public:secret)>` (Langfuse supports the OTel HTTP/protobuf and gRPC paths). (2) Set `OMNIGENT_TELEMETRY_ENABLED=true` and the OTEL envs in the deployment systemd drop-in. (3) **Disable switch**: `OMNIGENT_TELEMETRY_ENABLED=false` (or unset `OTEL_EXPORTER_OTLP_ENDPOINT`) makes the entire telemetry layer a no-op (upstream's `telemetry.py` already short-circuits). (4) **Secret filter**: the upstream `_redact_payload` already strips `authorization`, `*_token`, `*_secret`, `binding_token`. We add a deny-list header file (`deploy/control-room/langfuse/deny-headers.txt`) and a one-line call to `set_attribute` to drop those keys explicitly. |
| Harness Git/worktree conventions | (the operator knowledge that today lives in the deployment updater) | `deploy/control-room/harness/{pi,opencode}/git.md` + `worktree-conventions.md` | Operator docs. The Pi/OpenCode harnesses already run `git commit`, `gh pr create` from their worktrees; the convention page is the single source of truth. |
| Verity / polly agent YAML | `examples/control-room-polly/...` | `examples/polly/...` (no fork-specific extension) | The upstream `examples/polly/` already covers claude_code, codex, pi, opencode, cursor, hermes. The Control-Room layer only adds a top-level `verity` orchestrator that ties them together — see `examples/polly/config.yaml` for the documented composition. |
| Deployment / updater scripts | `scripts/{promote_release,rollback_release,write-dropin,_deployed_sha,deploy_status,cleanup_releases,install_omnigent_updater,omnigent_updater,promote_main_deploy,install_eval_*_unit}.sh` | `scripts/control-room/{promote,rollback,deploy_status,cleanup_releases,install_omnigent_updater,omnigent_updater,promote_main_deploy,install_eval_*_unit}.sh` + `scripts/control-room/_deployed_sha.sh` | Re-implementation lives in a sibling repo or `deploy/control-room/scripts/`. The `rebuild/upstream-0.7` worktree ships a **contract** (the same `version_sha`, `deployed-sha`, `_deployed_sha` helper semantics, the same drop-in invariants) and a thin **compatibility wrapper** in `scripts/control-room/` that imports nothing from `omnigent/`. |
| Acceptance tests | `tests/updater_acceptance/`, `tests/deploy/`, `tests/server/integration/test_*_provenance*.py` | `deploy/control-room/tests/` | Bounded pytest suites that exercise the control-room contract end-to-end without importing `omnigent/`. They live in the sibling repo or a `tests/control_room/` subdirectory of the rebuild branch. |
| Host-pin drop-in contract | `omnigent/deploy/ops/systemd.py`, the `install_eval_host_unit.sh` script, `_check_host_pinned` | `deploy/control-room/host-pin/` | The contract (cmdline-based argv check, no `/proc/<pid>/exe` reliance) is documented; the implementation is rewritten in the sibling repo. The fork's `omnigent/deploy/ops/systemd.py` is **not** ported to upstream. |
| The custom `omnigent/updater/` package | 3 500+ LOC | `deploy/control-room/updater/` (sibling repo) | Re-implement using the `omni upgrade` workflow contract: `gh workflow run upgrade.yml` for the source upgrade, plus a thin `promote_release.sh` that builds an immutable release, pins the systemd drop-in, runs the canary, and updates the deployed-sha marker. |
| Verity custom agent / Verity prompt | The fork's `7cc30b18` "fix(agents): switch control-room-polly top-level to claude-sdk" | `examples/polly/config.yaml` (verbatim upstream) | Upstream's `examples/polly/config.yaml` already includes a `claude-sdk` orchestrator that fans out to per-task subagents in their own worktrees. The fork's "verity" rename is purely cosmetic — drop it. |
| `omnigent/opencode_provenance_proxy.py` | Fork-only (1 file, ~370 LOC) | `deploy/control-room/omniroute/opencode_observer.py` | Re-implement as a small middleware in the Control-Room layer, importing only `httpx` and `fastapi` (already in pyproject). |
| `omnigent/runner/opencode_provenance.py`, `omnigent/runner/opencode_runtime_manager.py` | Fork-only | `deploy/control-room/omniroute/runner_hooks.py` | The hook is just two short callbacks; the implementation lives in Control-Room, with upstream's `opencode_native_*.py` providing the surface. |
| `omnigent/llms/routing.py` fork-specific provider entries (`"minimax"`, `"omniroute"`) | Fork-only | `~/.omnigent/config.yaml` `providers:` block | The upstream `OpenAICompatibleAdapter` already accepts `base_url` + `api_key` per call (see `connection_params`). No code change needed. |

### 2.3 Natively solved by upstream

| Concern | Upstream surface |
| --- | --- |
| OpenCode-native delegation resolver | `omnigent/harness_plugins.py` + `prune_invalid_sub_agents` |
| Smart routing judge | `omnigent/server/smart_routing.py` `LLMRoutingClient` |
| External `routes:select` | `omnigent/server/smart_routing.py` `ExternalRoutingClient` |
| Liveness-aware idle watchdog | **Not solved upstream** (this is the core upstream PR the rebuild ships — see §4.1) |
| Identity-scoped host registry deregister | **Not solved upstream** (this is the second core upstream PR — see §4.2) |
| OpenTelemetry master opt-in | `omnigent/runtime/telemetry.py` `OMNIGENT_TELEMETRY_ENABLED` |
| Per-session `session.id` span attribute | `_SessionIdSpanProcessor` (upstream) |
| W3C trace-context propagation across WS frames | `inject_trace_context` / `consume_frame_span` (upstream) |
| `x-api-key` / `authorization` / `*_token` redaction | `_redact_payload` (upstream) |
| `omni upgrade` + drain | `omnigent/cli.py` |
| `omni server --background` | `omnigent/cli.py` (post PR #3105) |

## 3. Configuration examples

### 3.1 OmniRoute as the only provider

```yaml
# ~/.omnigent/config.yaml
providers:
  omniroute:
    base_url: https://omniroute.omnigent-ai.example/v1
    api_key: ${OMNIROUTE_API_KEY}
  databricks:                       # for credential-managed Pi/Codex paths
    host: ${DATABRICKS_HOST}
    token: ${DATABRICKS_TOKEN}
llm:
  model: omniroute/MiniMax-M3
  profile: omniroute
harness:
  pi:
    model: HARNESS_PI_MODEL
```

`omnigent/llms/routing.py:25-31` accepts the custom prefix via
`PROVIDER_CONFIGS`; the upstream `OpenAICompatibleAdapter` already speaks
`base_url` + `api_key` per call (`omnigent/llms/adapters/openai.py:64-66`).
No code change required.

### 3.2 Verity / polly agent bundle

`examples/polly/agents/opencode/config.yaml` (upstream) already declares a
`opencode-native` worker with `gate_pushes: false`. The Control-Room
orchestrator `examples/polly/config.yaml` is stock upstream. **No custom
file needed.**

### 3.3 Langfuse enablement

```bash
# /etc/omnigent/control-room.env
OMNIGENT_TELEMETRY_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(public:secret)>
OTEL_SERVICE_NAME=omni-control-room-web
OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true
OMNIGENT_OTEL_HTTP_CLIENT_INSTRUMENTATION=true
OMNIGENT_OTEL_CAPTURE_CONTENT=false   # never in prod
```

```bash
# Disable switch: unset OTEL_EXPORTER_OTLP_ENDPOINT (or set
# OMNIGENT_TELEMETRY_ENABLED=false). Upstream's `telemetry.py` short-circuits
# both at module load and at every instrumentation site.
```

## 4. Unavoidable core patches

Two narrow patches are required because upstream has the exact bug they
fix. Each is small, isolated, and clearly upstream-quality.

### 4.1 Liveness-aware watchdog (re-implements `1e5ae9fb`)

- New `omnigent/runtime/harnesses/watchdog.py` (~700 LOC): `WatchdogBudgets`,
  `SupervisedSubprocess`, `TimeoutReason`, `classify_timeout`, the
  `register_supervised_subprocess` API.
- Add `SubprocessLivenessEvent` to `omnigent/server/schemas.py`.
- Extend `HarnessApp` in `omnigent/runtime/harnesses/_scaffold.py` with
  `_liveness_loop` (emit `subprocess_live` events) and replace the
  single-budget `idle_wd` with a 4-budget context.
- Add `omnigent/runtime/harnesses/watchdog.py` to the public surface and
  export the liveness API from `omnigent/runtime/harnesses/__init__.py`.
- **No** change to `process_manager.py` (already has the
  `OMNIGENT_HARNESS_IDLE_TIMEOUT_S` env hook).

**Tests:** port the fork's `tests/runtime/harnesses/test_watchdog_liveness.py`
(522 LOC, 27 tests). **Upstream PR title:**
`feat(harness): liveness-aware watchdog with per-subprocess liveness events`.

### 4.2 Identity-scoped host registry deregister (re-implements `ba19fb3b`)

- Add `deregister(connection: HostConnection | str) -> bool` to
  `omnigent/server/host_registry.py`. When called with a connection object,
  only remove the entry if `self._hosts.get(connection.host_id) is
  connection` (else the entry was replaced and we no-op).
- Switch `omnigent/server/routes/host_tunnel.py` disconnect paths to the
  identity-scoped form.
- Test: stale connection A cannot remove replacement B; mismatch
  connection ownership logs a warning and returns `False` (caller
  skips `set_offline`).

**Tests:** port the fork's `tests/server/test_host_registry.py` additions.
**Upstream PR title:** `fix(server): make HostRegistry.deregister
identity-scoped`.
