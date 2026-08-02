# Agent bundles — `deploy/rebuild/agents/`

Three agent bundle directories, each modeled on the upstream
`examples/polly/`, `examples/debby/`, `examples/aws_analyst/`
patterns and verified against upstream v0.7's spec loader.

## What's here

| Bundle | Harness | Role |
| --- | --- | --- |
| `verity/` | `claude-sdk` | The default orchestration agent. Plans, decomposes, dispatches to sub-agents, runs cross-vendor review. The `claude-sdk` harness resolves whatever Claude provider is configured (`omnigent setup` / `OPENAI_API_KEY` / Databricks / an OpenAI-compatible gateway). |
| `pi/` | `pi-native` | The REVIEW / EXPLORE specialist for read-mostly work. The only worker that can run any gateway model. |
| `opencode/` | `opencode-native` | The primary IMPLEMENTER for the production rebuild. |

Verity is registered as the **default agent** in both
`/var/lib/omnigent/config.yaml` (`default_agent: verity`) and
`/home/<service-user>/.omnigent/config.yaml` (`default_agent: verity`).
The two worker bundles (Pi, OpenCode) are referenced from Verity's
`tools.agents:` list.

## Why three and not the upstream five

Upstream's `examples/polly/` registers six workers (claude_code,
codex, opencode, cursor, hermes, pi). The production rebuild runs
**two** — Pi and OpenCode — because:

- The user explicitly listed **Pi** and **OpenCode** as the harnesses
  to recreate (`PHASED_IMPLEMENTATION_PLAN.md` §C.3).
- Cross-vendor review needs *at least two* AVAILABLE workers. Two
  workers (Pi + OpenCode) satisfy that exactly; adding more would
  multiply the harness-binary install surface area (each upstream
  worker requires a CLI: `claude`, `codex`, `cursor-agent`, `hermes`,
  `pi`, `opencode`) without changing what the rebuild actually
  exercises on the phone.
- The fork's prior production deploy had similar scope (Pi + OpenCode
  were the two primary workers; the rebuild preserves that surface).

If a future upstream release adds a worker we want, drop a new
bundle directory here and add it to Verity's `tools.agents:` list.
**No upstream code change is required.**

## Why the bundles are version-stable

Every bundle uses the upstream-canonical harness spelling
(`pi-native`, `opencode-native`, `claude-sdk`) and the
upstream-canonical executor config keys. No fork-specific fields,
no proprietary protocol knobs, no experimental flags. This means:

- **Future upstream updates**: a new upstream release changes the
  spec; the only thing we need to verify is that
  `spec_version: 1` and the keys we use are still valid. They are
  (every key we use is read by `omnigent/spec/parser.py` for at
  least the last three upstream minor versions).
- **Re-running `omni --agent <bundle>`** is idempotent
  (`omnigent/cli.py:_preregister_agent` — "Idempotent registration.
  Mirrors `omnigent.inner.cli._omnigent_register_yaml_bundle`"):
  re-registering on top of the same content hash updates
  `bundle_location` only when the content hash actually changed,
  so the row stays stable across no-op restarts.
- **Worktree lifecycle** is upstream-managed:
  `omnigent/server/routes/hosts.py:754-806` plus
  `omnigent/host/git_worktree.py`. The bundles do not need to
  configure worktree paths; the runner picks them up per session.

## What's NOT here

- **Sub-bundle `agents/<name>/` directories** (per-harness sub-bundle
  configs like upstream's `examples/polly/agents/opencode/`): we
  don't ship them because each Pi / OpenCode sub-session inherits
  its harness from the bundle name; an extra `agents/opencode/`
  layer is only useful when the orchestration agent dispatches to
  a *named sub-agent* distinct from the harness, which is not the
  production rebuild's pattern.
- **Worker-level `prompt:` overrides per-model**: the bundles pin
  no model, so each worker resolves whatever the configured
  provider's default model is (Pi picks Pi's default; OpenCode
  picks OpenCode's default). Per-task model selection happens at
  dispatch via `args.model` on the `sys_session_send` call.

## Install

```sh
sudo install -d -m 0755 -o root -g omnigent /srv/omnigent/agents
sudo cp -r deploy/rebuild/agents/* /srv/omnigent/agents/
sudo chown -R root:omnigent /srv/omnigent/agents
sudo find /srv/omnigent/agents -type f -exec chmod 0644 {} +
```

The systemd unit's `ExecStart=` then registers each bundle via
`--agent /srv/omnigent/agents/<name>` (see
`deploy/rebuild/systemd/omnigent.service.template`).

## Verify

```sh
omni --agent /srv/omnigent/agents/verity \
     --agent /srv/omnigent/agents/pi \
     --agent /srv/omnigent/agents/opencode \
     agent list --json | jq '.[] | {name, harness_kind}'
```

Expected: three rows; `verity.harness_kind == "claude-sdk"`,
`pi.harness_kind == "pi-native"`, `opencode.harness_kind == "opencode-native"`.