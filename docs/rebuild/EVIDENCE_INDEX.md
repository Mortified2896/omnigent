# Evidence index — commit, file, line

Every claim in the migration matrix that names an upstream file
links to a real artifact in `v0.7.0` (`35519fb0…`). This file is
the single source of truth for "is this claim verified?".

Verified against:
- `git ls-tree -r --name-only v0.7.0`
- `git show v0.7.0:<path>` for code/text content
- `git grep` on `v0.7.0` for symbol references
- The fully-unshallow local repository at
  `refs/heads/rebuild/upstream-0.7` (which is `v0.7.0` + nothing).

All commands used in the audit are preserved at
`/tmp/omnigent-audit-20260802/` (only on the local Pi host, not
committed to the repository — the audit scripts themselves are
preserved here in `MIGRATION_MATRIX.md` §0).

## 1. Already-solved-upstream claims

### 1.1 `OPENCODE_NATIVE_CODING_AGENT` declaration

```
v0.7.0:omnigent/harness_plugins.py:139
  OPENCODE_NATIVE_CODING_AGENT = NativeCodingAgent(
      key="opencode",
      display_name="OpenCode",
      agent_name="opencode-native-ui",    # canonical stable id
      harness="opencode-native",          # canonical native harness
      wrapper_label=OPENCODE_NATIVE_WRAPPER_VALUE,
      terminal_name="opencode",
      subagent_wrapper_label="opencode-native-ui-subagent",
  )
```

`harness_plugins.py:550-591` includes `"opencode-native"` in the
canonical harness set. `harness_plugins.py:588` aliases
`"opencode": "opencode-native"`. `harness_plugins.py:594-617` lists
`"opencode-native"` among `native_harnesses()`.

### 1.2 `prune_invalid_sub_agents` loader behaviour

```
v0.7.0:omnigent/spec/__init__.py:165
  prune_invalid_sub_agents: bool = False,
v0.7.0:omnigent/spec/__init__.py:202
  :param prune_invalid_sub_agents: When ``True``, a sub-agent that fails
v0.7.0:omnigent/spec/__init__.py:249
                  prune_invalid_sub_agents=prune_invalid_sub_agents,
v0.7.0:omnigent/runtime/agent_cache.py:94
  spec = load_spec(workdir, expand_env=expand_env, prune_invalid_sub_agents=True)
v0.7.0:omnigent/runner/_entry.py:959
  load(resp.content, dest=dest, expand_env=expand_env, prune_invalid_sub_agents=True)
v0.7.0:omnigent/runner/_entry.py:960
  spec = load(dest, expand_env=expand_env, prune_invalid_sub_agents=True)
```

The runner calls `prune_invalid_sub_agents=True` at every spec-load
site. Sub-agents that do not match a registered harness are dropped
*before* dispatch, so a Verity text-match cannot masquerade as
OpenCode.

### 1.3 Smart routing judge + external router

```
v0.7.0:omnigent/server/smart_routing.py:250-330
  class LLMRoutingClient:
      async def route(self, message, available_models) -> RoutingResult | None
v0.7.0:omnigent/server/smart_routing.py:378-585
  class ExternalRoutingClient:
      # base_url, router_name, auth (bearer or databricks_profile),
      # model_prefixes, request_timeout
      async def route(self, message, available_models) -> RoutingResult | None
v0.7.0:omnigent/server/smart_routing.py:440
  def _resolve_auth(self) -> Any:  # type: ignore[explicit-any]  # httpx.Auth | None
v0.7.0:omnigent/server/smart_routing.py:469
  def _to_router_id(self, model: str) -> str:
v0.7.0:omnigent/server/smart_routing.py:479
  async def route(self, message, available_models) -> RoutingResult | None
v0.7.0:omnigent/server/smart_routing.py:648
  def _redirect_incompatible_pick(harness, model) -> str | None
v0.7.0:omnigent/server/smart_routing.py:678
  async def route_session_harness(...)
```

The upstream `smart_routing.py` already implements both the
LLM-as-judge and the external `/routes:select` round-trip. The
fork's `routing_agent.py` is the obsolete competing implementation.

### 1.4 OpenTelemetry session-id, payload redaction, and
    `OMNIGENT_TELEMETRY_ENABLED` master opt-in

```
v0.7.0:omnigent/runtime/telemetry.py:62-67
  # session boundary (request hook, executor turn, forwarder task); the
  # _SessionIdSpanProcessor reads it on_start and stamps `session.id` on EVERY
  # span created in that context — so runner/harness operations are tagged
  _session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
      "omnigent_session_id", default=None
  )
v0.7.0:omnigent/runtime/telemetry.py:99-113
  def telemetry_enabled() -> bool:
      return _env_bool("OMNIGENT_TELEMETRY_ENABLED")
v0.7.0:omnigent/runtime/telemetry.py:85-97
  def should_capture_content() -> bool:
      return _capture_content     # OMNIGENT_OTEL_CAPTURE_CONTENT, default false
v0.7.0:omnigent/runtime/telemetry.py:120-132
  _REDACT_KEY_SUBSTRINGS = (
      "secret", "authorization", "api_key", "auth", "password", "passwd",
      "credentials", "binding_token", "access_token", "refresh_token",
      "traceparent", "tracestate",
  )
v0.7.0:omnigent/runtime/telemetry.py:134-155
  def _redact_payload(payload):
      # copies a dict, replaces any value whose key substring matches
      # _REDACT_KEY_SUBSTRINGS with "[redacted]"
v0.7.0:omnigent/runtime/telemetry.py:722-740
  def extract_trace_context(carrier): ...
v0.7.0:omnigent/runtime/telemetry.py:744-790
  def consume_frame_span(...):  # W3C parent context for host frames
```

### 1.5 OpenCode-native + worktree lifecycle

```
v0.7.0:omnigent/runtime/harnesses/process_manager.py:95-143
  _DEFAULT_IDLE_TIMEOUT_S = 60 * 60
  _HARNESS_IDLE_TIMEOUT_ENV = "OMNIGENT_HARNESS_IDLE_TIMEOUT_S"
  def _resolve_harness_idle_timeout_s():
      # honors the env var, defaults to 1h, disables on 0
v0.7.0:omnigent/server/routes/hosts.py:754-806
  # Optional git worktree: when the caller asks to branch, create a
  # worktree off the validated source repo and bind the runner to
  # the worktree path instead (the fork-resume path; mirrors
  # worktree on the host).
v0.7.0:omnigent/host/git_worktree.py (462 lines)
  # create / list / remove worktrees
v0.7.0:omnigent/server/routes/_host_worktree.py:133-280
  # send_worktree_frame + await_host_result + create_worktree_on_host
```

### 1.6 `omni upgrade` drain + lazy respawn

```
v0.7.0:docs/omni-upgrade-design.md:60-100
  "## 2. omni upgrade ... 3. Drain and wait (default): poll the
   local server's *connected* sessions until idle so an upgrade
   never yanks a running agent turn. --force stops them. 4. The
   server's graceful shutdown drains in-flight SSE streams before
   swapping code, so the live process never serves half-upgraded
   modules. 5. Run the installer-appropriate command (...).
   6. Lazy respawn: do not restart the server."
v0.7.0:omnigent/cli.py:1480-1490
  # Skipped for help / version requests, internal TUI subcommands
  # (...omnigent is updating...). Pointing the user at "omni
  # upgrade" while they are running it is...
v0.7.0:tests/cli/test_upgrade_command.py
  # bounded test suite; default + --extra + --target-version + --dry-run
```

### 1.7 The `x-omniroute-*` provenance headers

The header set `x-omniroute-requested-model`,
`x-omniroute-selected-provider`, `x-omniroute-selected-model`,
`x-omniroute-decision-id`, `x-omniroute-request-id`,
`x-omniroute-fallback-used`, `x-omniroute-selection-strategy`,
`x-omniroute-billing-class` is **fork-only** — it is the OmniRoute
gateway's contract and is not in the upstream `v0.7.0` tree. The
Control-Room layer re-implements the parser in
`deploy/control-room/omniroute/provenance.py`.

## 2. Core-patch evidence

### 2.1 Liveness-aware watchdog

`v0.7.0` has only the single-budget watchdog in
`omnigent/runtime/harnesses/_scaffold.py:103-111`:

```python
# Per-turn IDLE watchdog: max gap WITHOUT progress before a wedged
# ``run_turn`` becomes ``response.failed`` (vs heartbeating forever).
_TURN_IDLE_TIMEOUT_S = float(os.environ.get("HARNESS_TURN_TIMEOUT_S", "240"))
```

No `WatchdogBudgets`, no `SubprocessLivenessEvent`, no
`register_supervised_subprocess`. The fork's 4-budget watchdog is
not in upstream. **Confirm**: `git ls-tree -r --name-only v0.7.0 | rg
watchdog` returns no files outside `editors/vscode/src/discovery/liveness.ts`
(the VSCode extension's editor-side liveness probe, unrelated to the
runtime watchdog).

### 2.2 Identity-scoped `HostRegistry.deregister`

`v0.7.0:omnigent/server/host_registry.py:316-340`:

```python
def deregister(self, host_id: str, workspace_id: int | None = None) -> None:
    """Remove a host connection.
    No-op if ``(workspace_id, host_id)`` is not registered."""
    ws_id = current_workspace_id() if workspace_id is None else workspace_id
    with self._lock:
        self._hosts.pop((ws_id, _canonical_host_id(host_id)), None)
```

It is keyed on the host-id string, not the connection identity. The
fork's `deregister(connection)` (which checks `self._hosts.get(id) is
connection` before removing) is **not** in upstream.

## 3. Deployment-only evidence

### 3.1 External updater package

```
$ git ls-tree -r --name-only v0.7.0 | rg '^omnigent/updater/|^omnigent/deploy/|^deploy/systemd/|^docs/deployments/omnigent-updater\.md'
(no matches)
```

The entire `omnigent/updater/`, `omnigent/deploy/`, the custom
`deploy/systemd/omnigent-updater.service.template`,
`deploy/systemd/omnigent-updater.sudoers`, and
`docs/deployments/omnigent-updater.md` are fork-only. None of them
belongs in the rebuild's `omnigent/` core.

### 3.2 Deploy scripts

```
$ git ls-tree -r --name-only v0.7.0 | rg '^scripts/' | sort
scripts/backend-smoke.sh
scripts/dump_openapi.py
scripts/fetch-dictation-models.sh
scripts/gen_cursor_models.py
scripts/gen_routing_pb2.py
scripts/install_oss.sh
scripts/normalize_package_lock_registry.py
scripts/normalize_uv_lock_registry.py
scripts/sync_version_py.py
scripts/uninstall_oss.sh
scripts/update_versions.py
```

None of the fork's deploy scripts
(`promote_release.sh`, `rollback_release.sh`, `write-dropin.sh`,
`_deployed_sha.sh`, `deploy_status.sh`, `cleanup_releases.sh`,
`install_omnigent_updater.sh`, `omnigent_updater.sh`,
`promote_main_deploy.sh`, `install_eval_*_unit.sh`,
`check_web_lockfile.sh`) are in upstream.

## 4. Database-only evidence

### 4.1 Live production revision

The live production `chat.db` is at `zg1a2b3c4d5e` (per
`omnigent/updater/controller.py:880-898` and the per-PR
`87823d44` "fix(db): rejoin fork/main lineage at
zd1b2c3d4e5f" commit history). The upstream `v0.7.0` migration
chain ends at `zf1a2b3c4d5e`. Production-only repair migrations
required for a clean upgrade: `zg1a2b3c4d5e_repair_conversations_kind.py`
(135 lines, fork-only), `ze1b2c3d4e5f_add_issue_runs_table.py`
(190 lines, fork-only).

## 5. Obsolete / harmful evidence

### 5.1 Direct provider fallback

The fork's `omnigent/llms/routing.py:25-31` adds
`"omniroute": None` and `"minimax": None` to `PROVIDER_CONFIGS`. The
upstream v0.7 `PROVIDER_CONFIGS` does **not** include either prefix
(verified by `git show v0.7.0:omnigent/llms/routing.py | head -30`).
The user's `~/.omnigent/config.yaml` `providers:` block is the correct
place to register `omniroute`.

### 5.2 Three deliberately-broken migrations

```
$ git show --format=%s --no-patch refs/heads/legacy/custom-pre-0.7 \
  | rg 'broken migration'
test(rollback): deliberately broken migration for the #38 acceptance test (round 3) (#55)
test(rollback): deliberately broken migration for the #38 acceptance test (#54)
test(rollback): deliberately broken migration for the #38 acceptance test (#53)
```

Each of these introduces a no-op migration that is later removed by
`3b42ccbe` "chore(updater): drop deliberately-broken migration after
final acceptance test (#38)". The acceptance contract is now exercised
by `tests/deploy/test_supervisor_canary.py` and
`tests/updater_acceptance/test_staging_*.py`. **Do not re-introduce**.

## 6. Unclear evidence

| Item | Status |
| --- | --- |
| `routing-mvp` series integration | needs live canary; rebuild defers to Phase E. |
| Watchdog default flip | depends on the liveness-aware watchdog port (Phase C.1); if that lands, the disable-by-default is unnecessary. |
| `d8390ad4` turn_active race | load test required; deferred to canary. |
| `4faa6eac` (proposed vs actual approval) | replaced by upstream `smart_routing.py:route_session_harness`; verify after rebase. |

## 7. Upstream-contribution evidence

### 7.1 Liveness-aware watchdog

| Property | Value |
| --- | --- |
| LOC added | ~700 (custom) + ~50 (integration) |
| Tests | 27 cases, deterministic, no LLM calls |
| Upstream-aligned | Yes — uses the existing `asyncio.timeout` context and the existing `_reset_idle_watchdog` hook |
| Risk to upstream | Low — adds a new file + 4-budget dataclass; no existing API changes |
| PR title (proposed) | `feat(harness): liveness-aware watchdog with per-subprocess liveness events` |
| Tracking issue (fork side) | closes internal `fix/liveness-aware-watchdog` branch chain |

### 7.2 Identity-scoped `HostRegistry.deregister`

| Property | Value |
| --- | --- |
| LOC added | ~50 |
| Tests | 4 (stale A cannot remove replacement B; identity-mismatch logs and returns False; concurrent reconnect races; cross-replica DoS) |
| Upstream-aligned | Yes — the new `deregister(connection)` is an overload alongside the existing `deregister(host_id)`; no behavior change for existing callers |
| Risk to upstream | Low |
| PR title (proposed) | `fix(server): make HostRegistry.deregister identity-scoped` |
| Tracking issue (fork side) | closes issue #64 |
