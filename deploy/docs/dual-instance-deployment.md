# Omnigent Dual-Instance Acceptance — 2026-08-04

This document captures the acceptance evidence for the **Omnigent 1
(maintenance) and Omnigent 2 (production)** dual-instance deployment
that lives on host `ai-control-hub`.

It complements `architecture.md` with the specific IDs, paths, PIDs and
session logs that prove the two installations are independent and that
production self-recovers after a controlled server restart while
maintenance stays up at the same PID throughout.

## Deployed application source

| Field | Value |
|---|---|
| Repository | `https://github.com/Mortified2896/omnigent.git` |
| Local checkout | `/home/hermes/workspace/repos/omnigent-production` |
| Branch | `bootstrap/omnigent-production-2` |
| Repo HEAD | `94dccfcc8b2022a34f4487ddf1f330e1369e0db7` |
| Repo HEAD subject | `deploy: add Omnigent 2 production deployment artifacts` |
| Application source commit (deployed) | `28d5e1c6323dbeb2ee5f8cadff40b45de5f2224f` |
| Application source subject | `fix(telemetry): restore missing _fastapi_instrumentation_enabled helper` |

`94dccfcc` is one commit ahead of `28d5e1c6`; the diff is the deployment
artifacts (`deploy/docs/architecture.md`, `deploy/systemd/*.service`,
`deploy/scripts/deploy-omnigent-production.sh`,
`deploy/scripts/verify-omnigent-production.sh`). The committed
application source — what Omnigent production actually serves — is
`28d5e1c6`.

## Final URLs and isolation boundaries

| URL | Backend | Purpose |
|---|---|---|
| `https://hermes-agent.taile0361b.ts.net:1111/` | `127.0.0.1:4097` | Omnigent 1 maintenance (SPA index bundle: `index-CwPAK_En.js`) |
| `https://hermes-agent.taile0361b.ts.net:2222/` | `127.0.0.1:4197` | Omnigent 2 production (SPA index bundle: `index-VGJl5c8U.js`) |

The two bundles are different in two independent deployments of the same
Omnigent release; that is the visual fingerprint that distinguishes the
two backends.

Per-port Tailnet Serve mappings active after the acceptance pass:

| Mapping | Backend |
|---|---|
| `:443` | `127.0.0.1:3000` (pre-existing, unrelated) |
| `:1111` | `127.0.0.1:4097` (Omnigent 1) |
| `:2222` | `127.0.0.1:4197` (Omnigent 2) |
| `:6080` | `127.0.0.1:6080` (pre-existing, unrelated) |
| `:9443`, `:9445`, `:9446`, `:9447`, `:9450`, `:9451` | unrelated pre-existing mappings |
| `:9460` | `127.0.0.1:4096` (pre-existing, unrelated) |
| `:9462` | `127.0.0.1:5000` (pre-existing, unrelated) |
| `:20128` | `127.0.0.1:20128` (pre-existing, unrelated) |

The legacy `:9461` alias for the maintenance backend was removed with
`tailscale serve --https=9461 off`. **No** `tailscale serve reset` was
used — the only changed mapping is `:9461`. All other pre-existing
mappings were preserved.

## File-edit acceptance (Omnigent 2, custom/best-coding)

A disposable git repository was created at
`/var/lib/omnigent-production/tmp/disposable-omnigent-pi-test` (it
could not live under `/home/hermes` because the production host service
runs with `ProtectSystem=strict` + `ProtectHome=read-only`, leaving
`/home` read-only inside the runner mount namespace — see
`architecture.md` isolation invariant #6). The repository was seeded
with one tracked file:

```text
$ cat value.txt
before
$ git ls-files
value.txt
```

### Acceptance session

| Field | Value |
|---|---|
| Server URL | `https://hermes-agent.taile0361b.ts.net:2222/` |
| Session ID | `1e7f0160b618447ca4fc39c40276be47` |
| Runner ID | `runner_token_7d43a5f8ba7afe4c228f090b6cb91973` |
| Host ID | `ed5ddb6695a84c3091d8925b7394a6c4` (ai-control-hub) |
| Workspace | `/var/lib/omnigent-production/tmp/disposable-omnigent-pi-test` |
| Agent | `pi-native-ui` (harness `pi-native`) |
| Model | `custom/best-coding` (confirmed on Pi status line `↑28k ↓704 R21k CH42.8% 13.4%/128k (auto) custom/best-coding`) |
| Prompt transport | `POST /v1/sessions/{id}/events` with body `{type:"message", data:{role:"user", content:[{type:"input_text", text:"…"}]}}` (the server's hidden input dispatch path; `/v1/sessions/{id}/comments/send` is for the Web UI and does not relay to a headless runner) |

### Acceptance result

The Pi session executed `printf "after\n" > value.txt && wc -c value.txt && cat value.txt`.
The captured function-call output was:

```text
6 value.txt
after
```

State after the turn:

```text
$ cat value.txt
after
$ stat -c '%s bytes' value.txt
6 bytes
$ git status --short
 M value.txt
$ git diff
diff --git a/value.txt b/value.txt
index 90be1f3..294186e 100644
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-before
+after
$ git ls-files
value.txt
```

Only `value.txt` changed. The repo's `.git/` is untouched. The
acceptance pass verified the final assistant content text was exactly
`after`.

## Controlled production restart (Omnigent 2 self-recovery)

```text
# Before
omnigent.service            MainPID=1620126 (UNCHANGED throughout)
omnigent-host.service       MainPID=1614576 (UNCHANGED throughout)
omnigent-production.service MainPID=1671254
omnigent-production-host.service MainPID=1694097
```

Action: `systemctl restart omnigent-production.service` only
(`omnigent-production-host.service` not restarted).

```text
# After
omnigent.service            MainPID=1620126   (UNCHANGED)
omnigent-host.service       MainPID=1614576   (UNCHANGED)
omnigent-production.service MainPID=1761732   (changed: 1671254 -> 1761732)
omnigent-production-host.service MainPID=1694097   (UNCHANGED)
```

Verification after the restart:

- `GET https://hermes-agent.taile0361b.ts.net:2222/health` returned
  `{"status":"ok"}`.
- `GET https://hermes-agent.taile0361b.ts.net:2222/v1/info` returned the
  same `server_version: "0.7.0"` payload that the running release is
  pinned to.
- The acceptance session (`1e7f0160…`) returned to `status=idle` with
  `runner_online=True` and `host_online=True`, still bound to the same
  host (`ed5d…`) and the same runner (`7d43a5f8…`). A follow-up turn
  (`{role:"user", content:"Just print the exact contents of value.txt
  …"}`) ran the bash tool and the assistant text response was
  `after` — i.e. the runner reattached to its pre-existing Pi process
  and the original file change was preserved.

## What's in this repo

This doc plus the artifacts staged in the previous commit
(`94dccfcc8b2022a34f4487ddf1f330e1369e0db7`):

- `deploy/docs/architecture.md` — isolation invariants (this repo)
- `deploy/docs/dual-instance-deployment.md` — this document
- `deploy/scripts/deploy-omnigent-production.sh` — versioned release
  controller
- `deploy/scripts/verify-omnigent-production.sh` — post-deploy
  assertions

## What was not changed

- No restart, reinstall, or modification of `omnigent.service`,
  `omnigent-host.service`, `/opt/omnigent`, `/etc/omnigent`, or
  `/var/lib/omnigent`. Maintenance MainPIDs were `1620126` / `1614576`
  before, during and after every action.
- No change to `/etc/systemd/system/omnigent-production.service` or
  `/etc/systemd/system/omnigent-production-host.service` after
  acceptance step 5.
- No `tailscale serve reset` — only `tailscale serve --https=9461 off`.
- No HomeLab repo modifications.
