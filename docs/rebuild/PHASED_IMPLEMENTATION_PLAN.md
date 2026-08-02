# Phased implementation plan — in-place replacement

> **Goal:** the new upstream-based Omnigent 0.7 installation **becomes**
> the production instance on the existing website and the existing
> production port. There is no permanent second install, no second
> hostname, no second port, no second public endpoint.
>
> **The phone URL must keep working.** The only acceptable downtime is
> the brief restart required to replace the running service.

Each phase is gated on evidence and does not begin until the previous
phase's evidence is recorded. **Production is touched only by Phase
E**, which is a single stop → install → start on the existing systemd
unit (no second unit, no second port).

## Phase A — preservation (DONE in this audit)

A1. Verify repository state and produce `REPOSITORY_STATE_REPORT.md`.
A2. Create `legacy/custom-pre-0.7` (branch + tag) and
    `legacy/custom-pre-0.7-remote-main` (branch + tag) and
    `legacy/custom-pre-0.7-staged-20260802` (branch + tag for the dirty
    working-tree index).
A3. `git fetch --unshallow origin` (preserves `fetch_exit=0` log).
A4. Confirm the production fork and the new `rebuild/upstream-0.7`
    branch exist and that the upstream v0.7.0 tag SHA is recorded.

**Evidence committed to the rebuild branch:** `REPOSITORY_STATE_REPORT.md`,
`MIGRATION_MATRIX.md`, `ARCHITECTURE_PROPOSAL.md`, `OMNIROUTE_PLAN.md`,
`LANGFUSE_PLAN.md`, `HARNESS_GIT_PLAN.md`, `AUTO_UPDATE_PLAN.md`,
`ACCEPTANCE_TESTS.md`, `RISKS_AND_UNRESOLVED.md`,
`EVIDENCE_INDEX.md`, `README.md`, `AUDIT_REPORT.md`,
`PHASED_IMPLEMENTATION_PLAN.md` (this file).

## Phase B — architecture + plans (DONE in this audit)

B1. Author the four planning documents (OmniRoute, Langfuse, harness
    Git, auto-update) and the acceptance test specification. **Done.**
B2. Confirm the architecture fits the upstream v0.7 file inventory
    (no missing APIs, every cross-reference resolves). **Done.**
B3. Validate the upstream-only claims against the actual v0.7 tree.
    **Done.** Verified in particular that upstream v0.7:
    - already declares `OPENCODE_NATIVE_CODING_AGENT`
      (`omnigent/harness_plugins.py:139`) with `harness="opencode-native"`,
      and `prune_invalid_sub_agents=True` is set at every spec-load site
      (`omnigent/runner/_entry.py:959-960`, `omnigent/runtime/agent_cache.py:94,146,203`),
      so the fork's `agent_selector.py` and `opencode_provenance_proxy.py`
      are **not needed in the rebuild**.
    - already implements `ExternalRoutingClient` with bearer / Databricks
      profile auth, model-prefix rewriting, and `/routes:select` round-trip
      (`omnigent/server/smart_routing.py:378-585`). The fork's
      `routing_agent.py` is **not needed in the rebuild**.
    - already wires `OMNIGENT_TELEMETRY_ENABLED` as the master opt-in and
      `_SessionIdSpanProcessor` + `_redact_payload` as the per-span /
      per-payload guards (`omnigent/runtime/telemetry.py:62-160`,
      `275-330`). The fork's `telemetry_instrument.py` /
      `langfuse_exporter.py` are **not needed in the rebuild**.
    - already honors `OMNIGENT_HARNESS_TMP_PARENT` and
      `OMNIGENT_HARNESS_IDLE_TIMEOUT_S` (`omnigent/runtime/harnesses/process_manager.py:95-143`).
    - already exposes the worktree request flow used by the harness
      (`omnigent/server/routes/hosts.py:754-806`,
      `omnigent/host/git_worktree.py:462` lines,
      `omnigent/server/routes/_host_worktree.py:133-280`).

B4. Confirm in-place cutover is feasible against upstream v0.7's
    `omni server` CLI: it binds an explicit `--host`/`--port` on a
    real deploy (the canonical-local-server fast path is loopback-only
    and is bypassed on `--host 0.0.0.0` or an explicit `--port`
    — see `omnigent/cli.py:3192-3300` and the
    `_is_canonical_local_server` check). The systemd unit keeps the
    same `ExecStart=` shape; only the binary it invokes changes.

## Phase C — in-place configuration shims (REQUIRED before cutover)

This is **not** a port of fork behavior. It is a port of fork state
so the upstream wheel can boot on top of the existing `chat.db` and
the existing systemd unit. Three files, all small.

### C.1 — port the two fork-only Alembic migrations

Upstream v0.7's Alembic head is `zf1a2b3c4d5e`. The live `chat.db`
sits at `zg1a2b3c4d5e` (fork lineage: `zd1 → ze1 → zg1`). The two
fork-only migrations must be carried into the rebuild branch so
`alembic upgrade head` is a no-op when the new wheel boots the
production DB:

- `ze1b2c3d4e5f_add_issue_runs_table.py` (fork-only, 190 LOC, adds the
  `issue_runs` table the fork uses to track cross-session issue
  recovery). Renumber to a fresh revision id that sits *after*
  `zf1a2b3c4d5e` (e.g. `zh1a2b3c4d5e`).
- `zg1a2b3c4d5e_repair_conversations_kind.py` (fork-only, 135 LOC,
  backfills `conversations.kind`). Renumber to a fresh revision id
  that sits *after* `zh1a2b3c4d5e` (e.g. `zi1a2b3c4d5e`).

Both revisions carry `downgrade()` paths so the chain stays
bidirectional. Tests: `tests/db/test_alembic_no_op_on_live_db.py`
copies the live `chat.db` schema, runs `alembic upgrade head`, and
asserts no SQL is emitted; the symmetric `downgrade -1` round-trip is
also green.

> **This is the one real port from the fork** (Phase C of the
> earlier plan kept these two out of scope, but in-place cutover
> makes them mandatory). The SQL bodies are mechanical; the schema
> is the fork's, the behavior is the fork's, and we are just
> admitting that state into the new graph.

### C.2 — single EnvironmentFile for the systemd unit

Author `/etc/omnigent/env.conf` (POSIX, `EnvironmentFile=`-compatible)
containing every variable the existing fork unit sets:

```
# Auth — must match what the reverse proxy injects.
OMNIGENT_AUTH_PROVIDER=header
OMNIGENT_AUTH_HEADER=X-Forwarded-Email
# Cloudflare Access: use Cf-Access-Authenticated-User-Email instead.
# OMNIGENT_AUTH_HEADER=Cf-Access-Authenticated-User-Email

# Optional trust strip (Google IAP prefixes "accounts.google.com:").
# OMNIGENT_AUTH_HEADER_STRIP_PREFIX=accounts.google.com:

# Multi-user switch (the production deployment is multi-user).
OMNIGENT_AUTH_ENABLED=1

# Telemetry — master opt-in is per-process and is read from the env.
OMNIGENT_TELEMETRY_ENABLED=1
OMNIGENT_OTEL_CAPTURE_CONTENT=0
OTEL_EXPORTER_OTLP_ENDPOINT=https://langfuse.example.com/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<langfose_public_key>
OTEL_SERVICE_NAME=omnigent-prod

# Harness layout — keep exactly what the fork unit set.
OMNIGENT_HARNESS_TMP_PARENT=/srv/omnigent/harness-tmp
OMNIGENT_HARNESS_IDLE_TIMEOUT_S=3600

# Per-harness binary paths (only set the ones the fork unit set).
# OMNIGENT_PI_PATH=/usr/local/bin/pi
# OMNIGENT_OPENCODE_PATH=/usr/local/bin/opencode
# OMNIGENT_CLAUDE_PATH=/usr/local/bin/claude
# OMNIGENT_CODEX_PATH=/usr/local/bin/codex

# Index / data dir (defaults are fine if unset).
# OMNIGENT_INDEX_URL=https://pypi.org/simple
# OMNIGENT_DATA_DIR=/var/lib/omnigent
```

The EnvironmentFile is the **single source of truth**. Every phase
below reads from it; the fork wheel, the rebuild wheel, and any
future v0.7.x wheel all see the same config.

### C.3 — single systemd unit (replace, do not add)

The existing systemd unit at `/etc/systemd/system/omnigent.service`
already runs the service. We **edit the existing unit in place** —
we do not add a second one. The new `ExecStart=` is:

```
ExecStart=/usr/local/bin/omni server \
    --host 0.0.0.0 \
    --port <PROD_PORT> \
    --database-uri <PROD_DB_URI>
```

with `EnvironmentFile=/etc/omnigent/env.conf`. The unit's
`Restart=on-failure`, `User=`, `Group=`, `WorkingDirectory=`,
`LimitNOFILE=`, and any pre-start dependency (`After=network-online.target`)
are preserved verbatim. No port change. No hostname change.
No second unit. The unit is renamed back to the same name.

`systemctl daemon-reload` is run as part of the cutover.

### C.4 — idempotent in-place cutover script

`scripts/cutover.sh` (POSIX `sh`) implements the user's stated
sequence — stop → install → start → verify — with safety:

1. **Pre-flight:** read `EnvironmentFile`; verify the rebuild wheel
   file exists locally; verify the system has the matching DB URI
   reachable; verify the unit name; refuse to run unless `--confirm`
   is passed.
2. **Stop:** `systemctl stop omnigent` (the **single** existing unit).
3. **Install wheel:**
   - If the rebuild wheel is at a local path: `uv tool install --force
     /srv/omnigent/wheels/omnigent-0.7.0+rebuild-<sha>.whl` (or
     `pip install --force-reinstall <wheel>`).
   - If the rebuild is published to a private index: `uv tool install
     --force --index <index> omnigent==0.7.0+rebuild-<sha>`.
4. **Reload unit:** `systemctl daemon-reload`.
5. **Start:** `systemctl start omnigent`.
6. **Wait for `/health`:** poll `/health` for up to 45 s; fail closed
   if it does not return 200.
7. **Smoke-test in-process:** `/health`, `/api/sessions` (auth header
   injected from the EnvironmentFile), and `/api/whoami`.
8. **Stamp `deployed-sha`:** write `<rebuild-sha>` to the
   `/var/lib/omnigent/deployed-sha` marker (the same file the fork's
   `promote_release.sh` wrote to).
9. **Done.** Exit 0.

Idempotency: re-running the script on top of the same release
re-installs and re-stamps (a no-op). Re-running on top of a stale
release stops, installs the new wheel, restarts. Re-running on top of
a broken release (the `/health` wait times out) **does not** auto-
restore the previous fork wheel; that requires the explicit rollback
script (Phase G).

### C.5 — in-process smoke harness (15 tests)

The 15 acceptance tests in `docs/rebuild/ACCEPTANCE_TESTS.md` are
authored here as a single `pytest` suite that runs against the live
URL (with `--server-url https://<prod-hostname>` and a bearer
identity token or auth header). Tests cover web UI loads, login /
session, OmniRoute round-trip, Pi run, OpenCode run, Git commit, Git
push, Langfuse trace export, and the mobile-URL variant.

## Phase D — disposable-host canary

The canary is **not** a parallel production deployment. It is a
disposable VM (or local container) with:

- a fresh install of upstream `omnigent==0.7.0`,
- a snapshot of the live `chat.db` (copied via `sqlite3 chat.db .dump`
  to a fresh file so we do not mutate the live one),
- the same `EnvironmentFile` (C.2) symlinked in,
- the same systemd unit (C.3) symlinked in,
- the cutover script (C.4) run end-to-end on this disposable host.

The canary must demonstrate, in order:

- **D.1** `alembic upgrade head` against the live-DB snapshot is a
  no-op (proves the C.1 migration shim is correct).
- **D.2** `cutover.sh --confirm` stops, installs, starts, and
  `/health` returns 200 inside 45 s.
- **D.3** All 15 acceptance tests pass against the rebuild URL.
- **D.4** The cutover is **idempotent**: re-running it on top of the
  same release produces no diff (same wheel, same hash, same
  `deployed-sha`).
- **D.5** A simulated webhook load (50 turns, 5 concurrent) does not
  leak processes or connections.
- **D.6** A `journalctl -u omnigent -n 1000` capture is attached to
  `docs/rebuild/canary-report.md`.

The canary's host is destroyed after the canary; nothing about it
remains in production.

## Phase E — production cutover (THE SINGLE IN-PLACE REPLACEMENT)

This is the **only** step that touches production. It is a single
30–90 s window.

1. **E.1** Maintenance banner: configure the reverse proxy (or
   Cloudflare Access) to return a 503 with the maintenance HTML for
   ~60 s.
2. **E.2** `cutover.sh --confirm <rebuild-sha>` on the production
   host. The script:
   - stops the existing `omnigent.service`,
   - installs the rebuild wheel,
   - `systemctl daemon-reload`s,
   - starts the unit (same name, same port, same `--host 0.0.0.0`),
   - waits for `/health`,
   - runs the in-process smoke test,
   - writes `deployed-sha`.
3. **E.3** Remove the maintenance banner; confirm the phone URL loads
   the new SPA bundle (one curl from a CI host + one visual check
   from the phone).
4. **E.4** Capture `cutover-report.md` with: the pre-cutover
   `deployed-sha` (fork), the post-cutover `deployed-sha` (rebuild),
   the stop→start timestamps, the `/health` response, and the smoke-
   test output.
5. **E.5** Notify: text + a mobile Slack message "Omnigent 0.7
   rebuild cutover complete; old release preserved at
   `<pre-cutover-sha>`".

## Phase F — verification on the phone URL

Run the user's exact smoke-test list against the production URL.
Each is a single command captured to `cutover-report.md`:

| # | Check | Command |
| --- | --- | --- |
| 1 | Web UI loads | `curl -fsS https://<prod-hostname>/` |
| 2 | Login / session | `curl -fsS -H "X-Forwarded-Email: <identity>" https://<prod-hostname>/api/whoami` |
| 3 | OmniRoute works | one real `/v1/route` POST against the deployed router |
| 4 | Pi works | one real `pi-native` session, observe a turn complete |
| 5 | OpenCode works | one real `opencode-native` session, observe a turn complete |
| 6 | Git commits work | one real harness session that creates a commit |
| 7 | Git pushes work | one real harness session that pushes to a remote |
| 8 | Langfuse traces appear | one real session + grep the Langfuse API for `session.id` |
| 9 | Mobile access works | the user's phone opens the existing URL |

Each check is gated on green from Phase D. If any check fails, the
script proceeds to Phase G.

## Phase G — rollback (ready in advance, executed only on failure)

G1. **G.1** `systemctl stop omnigent`.
G2. **G.2** Re-install the previous fork wheel:
   `uv tool install --force --reinstall /srv/omnigent/wheels/omnigent-<previous-sha>.whl`
   (or the equivalent pip invocation).
G3. **G.3** `alembic downgrade -1` *only* if the cutover's
   `alembic upgrade head` actually moved the schema; with the C.1
   shim it should not have, so this is a belt-and-suspenders step.
G4. **G.4** `systemctl daemon-reload && systemctl start omnigent`.
G5. **G.5** Wait for `/health`. Confirm the previous `deployed-sha`
   is restored.
G6. **G.6** Notify: "Rollback to `<previous-sha>` complete; root
   cause: see `cutover-report.md` § failure."

The previous fork wheel is preserved at
`/srv/omnigent/wheels/omnigent-<previous-sha>.whl` from the audit
phase (the preservation commit `1b8f15039c…` plus a
`scripts/build-wheel.sh` artifact).

## Stop point for this audit

Per the original spec: "complete the investigation, preservation,
clean branch creation, migration matrix, architecture, and test
plan. You may implement low-risk scaffolding and tests on
rebuild/upstream-0.7, but do not port large groups of custom commits
or modify production until the audit clearly identifies what is
required." Phases A + B are complete in this audit. **No production
was modified.**

## Genuine blockers that would prevent an in-place cutover

These are the technical items that could force a parallel / blue-green
deployment *despite* the explicit "no second permanent install" goal.
Each one is something we must rule out in Phase D before Phase E.

### B-1. Database migration head mismatch (BLOCKER for now)

Upstream v0.7's Alembic head is `zf1a2b3c4d5e`. The live production
`chat.db` is at `zg1a2b3c4d5e` (fork lineage: `zd1 → ze1 → zg1`).
If we cut over without porting `ze1b2c3d4e5f_add_issue_runs_table.py`
and `zg1a2b3c4d5e_repair_conversations_kind.py`, the new wheel
boots, runs `alembic upgrade head`, sees the DB at
`zg1a2b3c4d5e`, and refuses with "no such revision: zg1a2b3c4d5e".
**Mitigation:** Phase C.1. Without C.1, an in-place cutover is
unsafe and a temporary parallel DB is the only option. This is the
only material blocker.

### B-2. Native-harness CLI version enforcement (CANARY-LEVEL)

Upstream PR #3335 (`92b1e10e`) added supported-CLI-version enforcement
for native harnesses. If the existing `pi`, `opencode`, `claude`,
`codex`, `cursor`, `kiro`, `goose`, `hermes`, `qwen`, or `kimi`
binaries on the production host are outside the upstream-supported
ranges, the new wheel will refuse to launch them. **Mitigation:**
capture the installed versions during Phase D; if any are out of
range, either upgrade them in place or pin the supported range via
the harness's `OMNIGENT_<NAME>_VERSION_OVERRIDE` knob. Not a
permanent blocker — can be resolved during the cutover.

### B-3. Auth-header name mismatch (CANARY-LEVEL)

If the reverse proxy injects `Cf-Access-Authenticated-User-Email`
(or some other non-default header) and the systemd unit does **not**
set `OMNIGENT_AUTH_HEADER`, every request will 401 and the phone
URL will look broken even though the server is healthy. **Mitigation:**
Phase C.2 forces a single EnvironmentFile; verify the header name
against the proxy config before the cutover. Resolvable in the unit
file, no second install needed.

### B-4. Custom `--artifact-location` outside `~/.omnigent/` (CANARY-LEVEL)

If the existing fork unit passes `--artifact-location
/srv/omnigent/artifacts`, the upstream wheel honors it (verified
in `omnigent/cli.py:3192-3300` — `--artifact-location` is preserved
verbatim). **Mitigation:** keep the flag in the new unit; verify
the path is writable. Not a blocker.

### B-5. Existing systemd unit runs a non-`omni` binary (CANARY-LEVEL)

If the existing fork unit runs `python -m omnigent` from a clone at
`/srv/omnigent/repo`, the rebuild wheel installs a `omni` shim to
`~/.local/bin/omni` (via `uv tool install`). The new unit must
point at the shim, not at the clone. **Mitigation:** Phase C.3
explicitly rewrites `ExecStart=` to `/usr/local/bin/omni server …`.
Resolvable, no second install needed.

### B-6. A running `--background` local server is already up (LOW RISK)

The upstream CLI's local-server pidfile machinery (`~/.omnigent/local_server.pid`)
is bypassed for `--host 0.0.0.0` and for an explicit `--port`, so a
real deploy never collides with the daemon's canonical local server.
But if the existing fork unit happens to be running on `127.0.0.1`
with the default port *and* the daemon was also running on the same
host, the daemon will see a stale pidfile and try to reuse a dead
PID. **Mitigation:** the cutover script (Phase C.4) runs
`omnigent server stop` before `systemctl stop` to clean up. Resolvable.

### B-7. Existing `chat.db` uses a custom SQLite cipher (BLOCKER-LEVEL IF PRESENT)

If the production `chat.db` was encrypted with SQLCipher (e.g.
the fork used `sqlcipher3-binary` with a key file), the upstream
wheel's SQLAlchemy will refuse to open it. **Mitigation:** none
without a backup-and-decrypt dance. We do not see evidence that
the production fork does this — verified by the absence of
`sqlcipher` references in any of the fork's deployment scripts —
but Phase D must confirm before Phase E.

## What is *not* a blocker (do not build for these)

- "What if we want to A/B test the new wheel against the fork?"
  Answer: not a blocker, just slow down Phase F's verification by
  adding a comparison step (e.g. a parallel non-public hostname
  serving the same wheel). If that comparison finds nothing, the
  comparison hostname is destroyed. We do not keep it.
- "What if we want a canary deployment that takes 5% of traffic?"
  Answer: the production traffic is one phone and a few laptops.
  A 5% canary is a marketing concept that does not map to a single-
  user load. The Phase D canary host is the canary.
- "What if we want the old wheel available without re-downloading?"
  Answer: the previous fork wheel is preserved at
  `/srv/omnigent/wheels/omnigent-<previous-sha>.whl` as part of
  Phase G. It is a *rollback artifact*, not a *second install*.