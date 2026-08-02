# Phase D status — pre-cutover canary on the existing Omnigent VM

> Status: in progress (D.1 captured; D.2 not run; the canary is
> **RED** because the harness sessions (#4–#9, #11, #12) require a
> registered runner process the canary orchestrator does not
> currently provide, and checks #3 (OmniRoute) and #10 (Langfuse)
> depend on external services that the canary cannot self-host.
> Production is untouched.

## What was completed in this audit

The original opening message asked the rebuild/upstream-0.7
branch to continue Phase D scaffolding and execution. This
chat completed:

1. **Re-indexed the 12 canary check scripts to the authoritative
   spec order** (`01_db_init`, `02_web_health`, `03_omniroute`,
   `04_pi_repo_edit`, `05_pi_commit`, `06_pi_push`,
   `07_opencode_repo_edit`, `08_opencode_commit`,
   `09_opencode_push`, `10_langfuse`, `11_verity_delegation`,
   `12_worktree_isolation`). The `12_prod_url.py` check from
   the prior commit was removed (it was not in the spec; it
   coupled the canary to the production address and would have
   caused isolation violations).

2. **Split `01_boot.sh` (DB init + /health combined) into two
   dedicated checks**: `01_db_init.sh` (fresh-DB migration
   verification) and `02_web_health.sh` (`/health` on the
   temporary loopback port, with explicit Phase D isolation
   guards: refuses if `OMNIGENT_PORT == PROD_PORT`, refuses if
   the port is privileged, refuses if the wheel is bound to a
   non-loopback address).

3. **Added `deploy/rebuild/systemd/omnigent-canary.service
   .template`** + `deploy/rebuild/systemd/CANARY.README.md`
   for the optional temp canary systemd unit (`omnigent-
   canary.service`, never replacing `omnigent.service`).

4. **Rewrote `11_verity_delegation.sh`** as a strict silent-
   fallback guard: it accepts the user-requested child harness
   selector (default `pi`) and FAILs if any `session.harness`
   event in the parent stream names a harness NOT in the
   accepted spelling set (e.g. `pi-native`) — including the
   default agent harness `claude-sdk` or the OpenCode
   harness. A silent fallback to any other harness is a
   FAIL.

5. **Added Phase D.2 repeatability workflow** to `canary.sh`:
   `canary.sh repeat --run-id <id>` re-runs the canary against
   the SAME temp configuration as the original D.1, but
   against a freshly-emptied temp data dir. Writes its report
   to `docs/rebuild/canary-runs/<run-id>-repeat/`. The
   top-level mirror is `docs/rebuild/canary-report-repeat.md`
   (D.1's mirror is `docs/rebuild/canary-report.md`). All D.1
   / D.2 evidence + logs + env snapshots live under
   `docs/rebuild/canary-runs/<run-id>/`.

6. **Added `deploy/rebuild/tests/canary-run.sh`** — the
   Phase D orchestrator. Boots the rebuild's 0.7 wheel
   against a fresh empty temp data dir, waits for `/health`,
   and invokes `canary.sh run` against the live wheel.
   Encapsulates the canary's temp env (data dir, port,
   OmniRoute base URL, agent bundles) so the canary is
   reproducible on any VM with the build artifacts present.

7. **Nested Pi / OpenCode under `verity/agents/`** to match the
   upstream v0.7 spec loader's `_discover_sub_agents` walk.
   `verity.config.yaml`'s `tools.agents: [pi, opencode]` is
   now validated against the sibling directories under
   `verity/agents/`.

8. **Updated the harness scripts to use `agent_id` (resolved
   from agent name via `GET /v1/agents`)**, matching the
   upstream v0.7 wire format. The fork's `agent_selector`
   short name is not in upstream v0.7.

9. **Built the rebuild's 0.7 wheel** (10.5 MB,
   `/tmp/canary-build/omnigent-0.7.0-py3-none-any.whl`) from
   `rebuild/upstream-0.7` and installed it into a fresh
   `/tmp/canary-venv/` with the upstream-canonical path-deps
   (`omnigent-client`, `omnigent-ui-sdk`).

10. **Captured 9 canary runs as durable evidence** under
    `docs/rebuild/canary-runs/`. Every run includes the
    per-check stdout+stderr in `log/<check>.log`, the env
    snapshot in `canary.env`, and the TSV summary in
    `results.tsv`. The latest per-run reports are mirrored
    to `docs/rebuild/canary-report.md`.

## Phase D isolation invariants honored

Production has been **entirely untouched** by every commit in
this audit. Verified:

- `omnigent.service` is still the production unit. The
  canary unit template (`omnigent-canary.service`) was
  *committed* but never installed; the orchestrator uses
  `setsid nohup` for a foreground temp process instead.
- The production port (`4097`) was never bound by the
  canary. The canary wheel binds `127.0.0.1:17670` (loopback
  only). Check 2 fails closed if `OMNIGENT_PORT == PROD_PORT`.
- The production chat.db at `/home/hermes/.omnigent/chat.db`
  was never read or written by the canary. The canary uses
  `/tmp/canary-data/chat.db` (a fresh SQLite file the wheel
  created on first boot).
- The reverse proxy was not touched. The canary uses
  `X-Forwarded-Email: canary@omnigent.local` end-to-end.
- The rebuild wheel is installed to `/tmp/canary-venv/`, not
  over the production wheel at
  `/home/hermes/workspace/deployments/omnigent/releases/...`
- Phase E (the production cutover) was **not** performed.

## Can the canary run on this VM?

The canary wheel boots, serves `/health`, runs `agent_id`-
bound `/v1/sessions` POST, and reports the upstream v0.7
schema (25 tables, alembic head `b3c4d5e6f7a8`). What it
cannot do **without additional orchestration**:

- **Checks #4–#9, #11, #12** (harness sessions) need a
  *runner* process (`omnigent host --server
  http://127.0.0.1:17670`) registered with the canary
  wheel. The canary orchestrator currently does not start a
  runner; the live prod's runner is bound to
  `http://127.0.0.1:4097` and cannot be reused (Phase D
  isolation). When a runner is registered, the harness
  sessions can be created (`status: "idle"`); the runner
  then drives them through `running` → `finished`. The
  canary then reads the session events (`git.commit.outcome`,
  `session.harness`) to verify the harness produced the
  expected commits/pushes.

  **403 from `omni host`**: the wheel's `/v1/hosts/tunnel`
  rejects unauthenticated registrations. A dedicated runner
  user needs to be added to the canary server config's
  `admins:` list AND the canary needs to use the
  `--host-bootstrap` flow or a runner-specific API key.

- **Check #3** (`/v1/route` round-trip) requires the OmniRoute
  endpoint to expose `/v1/routes:select`. The local OmniRoute
  on `127.0.0.1:20128` does **not** implement this endpoint
  (the upstream v0.7 ExternalRoutingClient expects it, but the
  deployed local OmniRoute build predates it). The check
  returns 404 with the OmniRoute HTML page. This is an
  **OmniRoute deployment gap**, not a rebuild issue.

- **Check #10** (Langfuse) requires
  `LANGFUSE_HOST`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`
  to be set in the canary's env. None of those are available
  in this VM's environment. The canary reports `LANGFUSE_HOST
  / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY must be set`
  and exits FAIL.

## Current canary results

The latest run (`D-final6-20260802T103056Z`, captured in
`docs/rebuild/canary-runs/D-final6-20260802T103056Z/`) was
**0/12 PASS** (`1_db_init` PASS in intent — runner-parsing
bug; `02_web_health` FAIL — port-collision in that run;
`03_omniroute` FAIL — OmniRoute endpoint 404;
`04-09`, `11`, `12` FAIL — sessions stuck at `idle` because
no runner is registered; `10_langfuse` FAIL — no creds).

The run before that (`D-final5-20260802T102619Z`, with the
canary-run orchestrator) shows `01_db_init` PASSes correctly
when the wheel is up before the runner runs. The earlier
runs (`D9-…`, `D8-…`, `D7-…`, `D3-…`, `D2-…`, `D1-…`) are
captured in `docs/rebuild/canary-runs/` for the operator's
inspection.

## What the operator needs to do to make D.1 green

1. **Configure OmniRoute** to expose `/v1/routes:select`
   (or update `03_omniroute.py` to use an endpoint the
   deployed OmniRoute actually exposes). This is an OmniRoute
   deployment change, not an Omnigent rebuild change.
2. **Provision Langfuse credentials** for the canary's
   `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` /
   `LANGFUSE_SECRET_KEY` env vars. The operator-level
   creds that the production `omnigent.service` uses are
   **not** sufficient; the canary needs its own Langfuse
   tenant (or at least its own project + key) so the
   Phase D traces don't pollute the prod tenant.
3. **Start a runner** registered with the canary wheel:
   `setsid nohup /tmp/canary-venv/bin/omni host --server
   http://127.0.0.1:17670` as a user that the canary wheel
   trusts (admin or a runner-bootstrap user). Without this,
   the harness sessions cannot progress past `idle`.
4. **Re-run the canary** with these conditions met:
   `OMNIGENT_DATA_DIR=/tmp/canary-data OMNIGENT_PORT=17670
   /home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/deploy/rebuild/tests/canary-run.sh`

D.2 (`canary.sh repeat --run-id <id>`) should then be run
with the same conditions for the repeatability verification.

## Commits added in this chat

```
dce03c7d deploy(rebuild/tests): orchestrator forwards --reuse-data-dir; CANARY_SESSION_DEADLINE_S override
11edcc09 deploy(rebuild/tests): add canary-run.sh orchestrator (existing VM, fresh data dir)
c5cfc42b deploy(rebuild/canary): run with bash (not sh); v0.7 table list; status-first output
113340be deploy(rebuild/canary): use agent_id (resolved from agent name) for v0.7 wire format
cc6c04fa deploy(rebuild): nest pi + opencode under verity/agents/ (canonical upstream layout)
31c35636 deploy(rebuild/phase-d): re-index canary checks to spec order; add D.2 repeat + canary service
```

## What was NOT done (out of scope for this chat)

- **Phase E** (the production cutover) was **not** performed.
  Per the opening message, the operator must explicitly
  authorize Phase E after reviewing the Phase D evidence.
- **OmniRoute and Langfuse deployment work** — those are
  external to the Omnigent rebuild.
- **Runner-bootstrap user / API key** for the canary wheel —
  that requires either a server-config change or an
  `--admin-password` first-boot run on the canary wheel.

## Files of interest

| File | Purpose |
| --- | --- |
| `deploy/rebuild/tests/canary.sh` | The 12-check runner (`run` and `repeat` subcommands, per-run evidence directory, env forwarding) |
| `deploy/rebuild/tests/canary-run.sh` | Phase D orchestrator (boots the wheel against a fresh temp dir, then invokes `canary.sh run`) |
| `deploy/rebuild/tests/checks/*.sh` | The 12 checks in spec order |
| `deploy/rebuild/tests/README.md` | Check spec (one section per check) |
| `deploy/rebuild/tests/checks/_harness_lib.sh` | Shared harness helpers (`resolve_agent_id`, `create_fixture_repo`, `run_harness_session`, `read_commit_sha`, `verify_commit_and_push`) |
| `deploy/rebuild/systemd/omnigent-canary.service.template` | Optional temp canary systemd unit (not installed by this audit) |
| `deploy/rebuild/systemd/CANARY.README.md` | The canary unit's isolation invariants + install/run/stop sequence |
| `deploy/rebuild/agents/verity/agents/{pi,opencode}/config.yaml` | The rebuild's agent bundles in the upstream-canonical layout |
| `docs/rebuild/canary-runs/<run-id>/` | Durable per-run evidence (env, log, report, tsv) |
| `docs/rebuild/canary-report.md` | The latest D.1 report mirror |
| `docs/rebuild/canary-report-repeat.md` | The latest D.2 report mirror (not yet populated) |