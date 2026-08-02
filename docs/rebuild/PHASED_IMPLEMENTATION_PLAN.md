# Phased implementation plan — in-place replacement on a fresh 0.7 database

> **Goal:** the new upstream-based Omnigent 0.7 installation **becomes**
> the production instance on the existing website and the existing
> production port. There is no permanent second install, no second
> hostname, no second port, no second public endpoint.
>
> **The phone URL must keep working.** The only acceptable downtime is
> the brief restart required to replace the running service.
>
> **Database strategy:** the live production `chat.db` is **disposable**.
> The existing fork-lineage schema is **not** migrated into upstream
> v0.7. During the cutover, the existing `chat.db` is **moved aside**
> to a timestamped, read-only backup, and a brand-new upstream 0.7
> database is initialized at the same configured database URI. No
> sessions, agents, scheduled tasks, conversation records, or database
> configuration are imported from the legacy database. Only the items
> the user explicitly listed below are recreated from files /
> environment.

Each phase is gated on evidence and does not begin until the previous
phase's evidence is recorded. **Production is touched only by Phase E**,
which is a single stop → backup-and-move → install → start on the
existing systemd unit (no second unit, no second port).

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
    - already auto-runs `alembic.command.upgrade("head")` on first
      engine creation against a database with no `alembic_version`
      table (`omnigent/db/utils.py:292-360, 460-510`,
      `_initialize_or_verify_schema`). A fresh `chat.db` is created
      and brought to head with no operator action — there is no
      manual `init-db` step required.
    - already supports `--database-uri`, `--artifact-location`,
      `--host 0.0.0.0`, an explicit `--port`, `--admin-password`,
      `--no-open`, and `--background` on `omni server`
      (`omnigent/cli.py:3103-3210, 3192-3300`).
    - already bypasses the canonical-local-server fast path on any
      real deploy (`_is_canonical_local_server` is `True` only for
      loopback + default DB + default artifacts + non-explicit
      port; the production deploy is none of those).

B4. Confirm in-place cutover is feasible on a **fresh 0.7 database**:
    moving the live `chat.db` out of the configured database URI
    and starting the new wheel triggers upstream's own first-boot
    Alembic migration path (`_initialize_or_verify_schema`), which
    creates the database file if needed, brings the schema to head,
    and exits cleanly. No fork migrations are required.

## Phase C — runtime configuration recreation

This phase **does not touch the database**. It captures the runtime
configuration that must persist across the cutover, as files the
systemd unit sources on every start. None of these items is imported
from the legacy database.

### C.1 — single EnvironmentFile for the systemd unit

Author `/etc/omnigent/env.conf` (POSIX, `EnvironmentFile=`-compatible)
containing every variable the existing fork unit sets. This is the
**single source of truth** for runtime config; the new wheel reads
the same file on every start.

```
# --- Auth (must match what the reverse proxy injects) ----------
OMNIGENT_AUTH_PROVIDER=header
OMNIGENT_AUTH_HEADER=X-Forwarded-Email
# Cloudflare Access: use Cf-Access-Authenticated-User-Email instead.
# OMNIGENT_AUTH_HEADER=Cf-Access-Authenticated-User-Email

# Optional trust strip (Google IAP prefixes "accounts.google.com:").
# OMNIGENT_AUTH_HEADER_STRIP_PREFIX=accounts.google.com:

# Multi-user switch (the production deployment is multi-user).
OMNIGENT_AUTH_ENABLED=1

# Admin roster (one identity per line, in <data_dir>/admins).
OMNIGENT_ADMIN_LIST_PATH=/etc/omnigent/admins

# --- Data dir (explicit so a fresh DB lands in the expected path) -
# Use OMNIGENT_DATA_DIR to pin the data dir; the cutover script
# creates /var/lib/omnigent if missing and points chat.db at it.
OMNIGENT_DATA_DIR=/var/lib/omnigent

# --- Telemetry / Langfuse (OpenTelemetry exporter) --------------
OMNIGENT_TELEMETRY_ENABLED=1
OMNIGENT_OTEL_CAPTURE_CONTENT=0
OTEL_EXPORTER_OTLP_ENDPOINT=https://langfuse.example.com/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<langfuse_public_key>
OTEL_SERVICE_NAME=omnigent-prod

# --- Harness layout (keep exactly what the fork unit set) -------
OMNIGENT_HARNESS_TMP_PARENT=/srv/omnigent/harness-tmp
OMNIGENT_HARNESS_IDLE_TIMEOUT_S=3600

# Per-harness binary paths (only set the ones the fork unit set).
# OMNIGENT_PI_PATH=/usr/local/bin/pi
# OMNIGENT_OPENCODE_PATH=/usr/local/bin/opencode
# OMNIGENT_CLAUDE_PATH=/usr/local/bin/claude
# OMNIGENT_CODEX_PATH=/usr/local/bin/codex

# --- Index / package source (defaults are fine if unset) -------
# OMNIGENT_INDEX_URL=https://pypi.org/simple
```

### C.2 — server config (`<data_dir>/config.yaml`)

Upstream v0.7 reads `<data_dir>/config.yaml` (default
`/var/lib/omnigent/config.yaml` when `OMNIGENT_DATA_DIR=/var/lib/omnigent`)
on server boot. The file holds **non-secret settings only**; secrets
stay in the EnvironmentFile. Author this file to match the production
deploy:

```yaml
# /var/lib/omnigent/config.yaml
# Non-secret server settings. Source of truth for what is not in
# EnvironmentFile (admins, allowed domains, harness policy modules,
# artifact location, host/port, database URI, etc.).

# OmniRoute gateway — the OpenAI-compatible provider entrypoint.
providers:
  - name: omniroute
    type: openai-compatible
    base_url: https://omniroute.example.com/v1
    api_key_env: OMNIROUTE_API_KEY
    request_timeout_s: 60

# Default routing model for the smart-routing judge.
routing:
  judge_model: omniroute/auto
  fallback_model: omniroute/auto
  approval:
    default: approve
    # strict-mode is off so the phone workflow does not stall
    # on a missing approval hook.

# Allowed domains for the OIDC / accounts provider (not used by the
# header-auth mode, but kept for future migration).
allowed_domains:
  - example.com

# Admin list lives in /etc/omnigent/admins, not here. The
# OMNIGENT_ADMIN_LIST_PATH env var pins the path.

# Artifact + harness tmp layout (mirrors the EnvironmentFile; the
# CLI flags win when both are set).
artifact_location: /srv/omnigent/artifacts
harness_tmp_parent: /srv/omnigent/harness-tmp

# Database URI (mirrors what `omni server --database-uri` will pass).
# A fresh file is created on first boot.
database_uri: sqlite:////var/lib/omnigent/chat.db
```

### C.3 — recreate runtime configuration from files / environment

The user explicitly listed the items that must be recreated. Each one
maps to a concrete file or environment variable the rebuild wheel
honors on first boot. **None of them is imported from the legacy
database.**

| Item | Where it lives | How it is recreated |
| --- | --- | --- |
| OmniRoute gateway + route settings | `<data_dir>/config.yaml` `providers:` + `routing:` blocks (above); `OMNIROUTE_API_KEY` in the EnvironmentFile | Authored from scratch in C.2; never reads the legacy DB. |
| Langfuse / OTLP | `OTEL_EXPORTER_OTLP_*` + `OMNIGENT_TELEMETRY_ENABLED=1` in the EnvironmentFile (C.1) | Authored from scratch in C.1. |
| Authentication header | `OMNIGENT_AUTH_PROVIDER=header`, `OMNIGENT_AUTH_HEADER`, `OMNIGENT_AUTH_ENABLED=1` in the EnvironmentFile (C.1); admin roster in `/etc/omnigent/admins` | Authored from scratch in C.1. |
| Pi harness setup | `OMNIGENT_PI_PATH` env var (if pinned); per-user `~/.config/pi/` (outside the database); `omni --agent /srv/omnigent/agents/pi-native-ui` if a custom bundle is shipped | Set in EnvironmentFile; user's home-directory config preserved by the cutover (it lives outside the DB). |
| OpenCode harness setup | `OMNIGENT_OPENCODE_PATH` env var; per-user `~/.config/opencode/`; `omni --agent /srv/omnigent/agents/opencode-native-ui` if a custom bundle is shipped | Set in EnvironmentFile; user's home-directory config preserved. |
| Git identity and GitHub credentials | Per-user `~/.gitconfig`, `~/.config/gh/`, and the SSH keyring (`~/.ssh/`) | **Preserved by the cutover** — they live outside the DB and outside the wheel. No recreation needed. |
| Custom agent definitions (Verity, etc.) | Bundle directories under `/srv/omnigent/agents/`; registered on the systemd `ExecStart=` via `omni --agent <bundle-path>` | Authored from scratch in C.3.1; the legacy DB rows are NOT imported. |
| Artifact directory | `--artifact-location /srv/omnigent/artifacts` (CLI flag) or `artifact_location:` in `config.yaml` | Authored from scratch; the legacy artifacts are NOT imported. |
| Harness temporary directory | `OMNIGENT_HARNESS_TMP_PARENT=/srv/omnigent/harness-tmp` (env var) | Authored from scratch in C.1; the legacy tmp contents are discarded. |

#### C.3.1 — agent bundle directory

Author `/srv/omnigent/agents/` containing the upstream-shipped agent
bundles the production deployment uses. These are the same bundles
the user currently has on disk in the fork's checkout; copy them
verbatim into the rebuild's `/srv/omnigent/agents/` and register
them on the systemd `ExecStart=`. Do not migrate any per-agent state
(rows in the legacy `agents` table) — recreate the bundles from the
on-disk YAMLs only.

The systemd `ExecStart=` is updated to register each bundle:

```
ExecStart=/usr/local/bin/omni server \
    --host 0.0.0.0 \
    --port <PROD_PORT> \
    --database-uri sqlite:////var/lib/omnigent/chat.db \
    --artifact-location /srv/omnigent/artifacts \
    --config /var/lib/omnigent/config.yaml \
    --agent /srv/omnigent/agents/verity \
    --agent /srv/omnigent/agents/pi-native-ui \
    --agent /srv/omnigent/agents/opencode-native-ui
```

`--agent` is repeatable and idempotent: re-running the unit on the
same bundle does not re-create the agent row.

### C.4 — single systemd unit (replace, do not add)

The existing systemd unit at `/etc/systemd/system/omnigent.service`
already runs the service. We **edit the existing unit in place** —
we do not add a second one. The new `ExecStart=` shape is shown in
C.3.1; the unit's `Restart=on-failure`, `User=`, `Group=`,
`WorkingDirectory=`, `LimitNOFILE=`, and `After=network-online.target`
are preserved verbatim. No port change. No hostname change. No
second unit.

`EnvironmentFile=/etc/omnigent/env.conf` is added (it may already
exist on the existing unit; if so, the file is the same one). The
admin roster file `/etc/omnigent/admins` is preserved at its existing
path.

`systemctl daemon-reload` is run as part of the cutover.

### C.5 — idempotent in-place cutover script

`scripts/cutover.sh` (POSIX `sh`) implements the user's stated
sequence — **stop → backup the existing DB → move it aside →
install the new wheel → start → verify** — with safety:

1. **Pre-flight:** read `EnvironmentFile`; resolve the configured
   database URI; resolve the rebuild wheel file; verify the
   systemd unit name; refuse to run unless `--confirm` is passed.
2. **Stop:** `systemctl stop omnigent` (the **single** existing
   unit). Wait for the unit to be `inactive` (or `failed`).
3. **Backup the existing database.** Resolve the configured DB
   URI's path; if the file exists:
   1. Compute its SHA-256 and byte size.
   2. `mkdir -p /var/lib/omnigent/backups`.
   3. `cp --reflink=auto -p <DB_PATH>
      /var/lib/omnigent/backups/chat.db.<UTC-timestamp>.bak`.
   4. `chmod 0444 /var/lib/omnigent/backups/chat.db.<ts>.bak`
      (read-only; cannot be accidentally written to).
   5. `chattr +i /var/lib/omnigent/backups/chat.db.<ts>.bak`
      (immutable; cannot be deleted or overwritten without an
      explicit `chattr -i`).
   6. Verify the backup:
      - File exists, size > 0.
      - Recorded SHA-256 matches the live file's pre-copy SHA-256.
      - File is non-empty (`stat -c %s`).
      - `sqlite3 <backup> ".schema"` returns a non-empty schema
        (proves it is a real SQLite database, not a partial copy).
4. **Move the old live database out of the active database path.**
   `mv <DB_PATH> <DB_PATH>.pre-0.7.<UTC-timestamp>` (do **not**
   delete it). The file is preserved at the same path with a
   timestamped suffix for forensic inspection; the cutover
   script records this path in `cutover-report.md`.
5. **Install the rebuild wheel:**
   - If the rebuild wheel is at a local path: `uv tool install
     --force /srv/omnigent/wheels/omnigent-0.7.0+rebuild-<sha>.whl`
     (or `pip install --force-reinstall <wheel>`).
   - If the rebuild is published to a private index: `uv tool
     install --force --index <index> omnigent==0.7.0+rebuild-<sha>`.
6. **Reload unit:** `systemctl daemon-reload`.
7. **Start:** `systemctl start omnigent`.
8. **Wait for `/health`:** poll `/health` for up to 45 s; fail
   closed if it does not return 200.
9. **Initialize the fresh database.** The new wheel's first
   engine connection runs `alembic.command.upgrade("head")`
   automatically (`omnigent/db/utils.py:460-510`); the cutover
   script confirms this by capturing `journalctl -u omnigent -n 50`
   and asserting it contains a "Running database migrations…"
   log line and no "schema is out of date" error.
10. **Smoke-test in-process:** `/health`, `/api/whoami`
    (auth header injected from the EnvironmentFile), and
    `POST /v1/sessions` to confirm the fresh DB accepts a
    new session.
11. **Stamp `deployed-sha`:** write `<rebuild-sha>` to the
    `/var/lib/omnigent/deployed-sha` marker (the same file the
    fork's `promote_release.sh` wrote to, preserved at its
    existing path; the cutover script writes the new value).
12. **Done.** Exit 0.

Idempotency: re-running on top of the same release re-installs
the wheel, re-stamps `deployed-sha`, and re-runs the smoke test
(no-op). Re-running after a failure rolls the backup forward
(`<DB_PATH>.pre-0.7.<ts>` becomes the new `<DB_PATH>`) and tries
again — but only on explicit operator action (`--confirm`).

### C.6 — fresh-DB acceptance test suite

The 15 acceptance tests in `docs/rebuild/ACCEPTANCE_TESTS.md` are
authored here as a `pytest` suite that runs against the live URL
(with `--server-url https://<prod-hostname>` and a bearer identity
token or auth header). The suite assumes a **fresh 0.7 database**:
no row-count snapshots, no pre-existing sessions, no legacy schema
assertions. See `ACCEPTANCE_TESTS.md` §1 for the full list and
`ACCEPTANCE_TESTS.md` §"Fresh-DB mode" for the changes from the
previous 15-test spec.

## Phase D — disposable-host canary

The canary is **not** a parallel production deployment. It is a
disposable VM (or local container) with:

- a fresh install of upstream `omnigent==0.7.0`,
- a **fresh** empty database at the same `<data_dir>/chat.db`
  path the production deploy uses,
- the same `EnvironmentFile` (C.1) symlinked in,
- the same `<data_dir>/config.yaml` (C.2) symlinked in,
- the same systemd unit (C.4) symlinked in,
- the cutover script (C.5) run end-to-end on this disposable host.

The canary must demonstrate, in order:

- **D.1** `cutover.sh --confirm` on an empty data dir:
  - `/health` returns 200 inside 45 s.
  - `journalctl -u omnigent -n 50` contains
    `"Running database migrations…"` (proves the first-boot
    migration path fired) and no `"schema is out of date"`
    error.
  - `sqlite3 /var/lib/omnigent/chat.db ".tables"` returns a
    non-empty list (proves the fresh schema is in place).
- **D.2** All 15 acceptance tests pass against the rebuild URL.
- **D.3** The cutover is **idempotent**: re-running it on top of
  the same release produces no diff (same wheel, same hash, same
  `deployed-sha`).
- **D.4** A simulated webhook load (50 turns, 5 concurrent) does
  not leak processes or connections.
- **D.5** A `journalctl -u omnigent -n 1000` capture is attached to
  `docs/rebuild/canary-report.md`.

The canary's host is destroyed after the canary; nothing about it
remains in production. **The canary never sees the production
database; it uses a fresh empty database.**

## Phase E — production cutover (THE SINGLE IN-PLACE REPLACEMENT)

This is the **only** step that touches production. It is a single
30–90 s window.

1. **E.1** Maintenance banner: configure the reverse proxy (or
   Cloudflare Access) to return a 503 with the maintenance HTML
   for ~60 s.
2. **E.2** `cutover.sh --confirm <rebuild-sha>` on the production
   host. The script:
   - stops the existing `omnigent.service`,
   - backups the existing `chat.db` to
     `/var/lib/omnigent/backups/chat.db.<UTC-ts>.bak`
     (read-only, immutable),
   - verifies the backup (size, sha256, sqlite schema),
   - moves the live `chat.db` out of the configured path as
     `<DB_PATH>.pre-0.7.<UTC-ts>`,
   - installs the rebuild wheel,
   - `systemctl daemon-reload`s,
   - starts the unit (same name, same port, same
     `--host 0.0.0.0`),
   - waits for `/health`,
   - confirms the first-boot migration log line,
   - runs the in-process smoke test (`/health`, `/api/whoami`,
     `POST /v1/sessions`),
   - writes `deployed-sha`.
3. **E.3** Remove the maintenance banner; confirm the phone URL
   loads the new SPA bundle (one curl from a CI host + one visual
   check from the phone).
4. **E.4** Capture `cutover-report.md` with:
   - the pre-cutover `deployed-sha` (fork),
   - the post-cutover `deployed-sha` (rebuild),
   - the backup's path, size, sha256,
   - the moved-aside DB path,
   - the stop→start timestamps,
   - the `/health` response,
   - the smoke-test output,
   - the journalctl first-boot migration log lines.
5. **E.5** Notify: text + a mobile Slack message "Omnigent 0.7
   rebuild cutover complete; old release preserved at
   `<backup-path>` (read-only)".

## Phase F — verification on the phone URL

Run the user's exact smoke-test list against the production URL.
Each is a single command captured to `cutover-report.md`:

| # | Check | Command |
| --- | --- | --- |
| 1 | Database initializes | `journalctl -u omnigent -n 50 \| grep "Running database migrations"` + `sqlite3 /var/lib/omnigent/chat.db ".tables"` |
| 2 | Web UI loads | `curl -fsS https://<prod-hostname>/` |
| 3 | A new session can be created | `curl -fsS -H "X-Forwarded-Email: <identity>" -X POST https://<prod-hostname>/v1/sessions` |
| 4 | Pi works | one real `pi-native` session, observe a turn complete |
| 5 | OpenCode works | one real `opencode-native` session, observe a turn complete |
| 6 | Git commits work | one real harness session that creates a commit |
| 7 | Git pushes work | one real harness session that pushes to a remote |
| 8 | OmniRoute provenance works | one real `/v1/route` POST against the deployed router; observe the `x-omniroute-*` response headers |
| 9 | Langfuse receives traces | one real session + grep the Langfuse API for `session.id` |
| 10 | Mobile access works | the user's phone opens the existing URL |

Each check is gated on green from Phase D. If any check fails,
the script proceeds to Phase G.

## Phase G — rollback (ready in advance, executed only on failure)

Rollback **does not** restore the fork wheel or the legacy
database. The fork's database state is disposable; the legacy
fork wheel is historical. Rollback means reverting to a previous
rebuild release (the one captured in `deployed-sha` immediately
before this cutover) if a previous rebuild was running.

G1. **G.1** `systemctl stop omnigent`.
G2. **G.2** Move the current (fresh, possibly-broken) DB out of
   the path: `mv <DB_PATH> <DB_PATH>.failed-<ts>`. (Do NOT
   delete; preserve for inspection alongside the pre-0.7 backup.)
G3. **G.3** Initialize a fresh DB at the configured path; the
   new wheel's first-boot migration path creates it.
G4. **G.4** Re-install the previous rebuild wheel:
   `uv tool install --force --reinstall /srv/omnigent/wheels/omnigent-<previous-rebuild-sha>.whl`
   (or the equivalent pip invocation).
G5. **G.5** `systemctl daemon-reload && systemctl start omnigent`.
G6. **G.6** Wait for `/health`. Confirm the previous
   `deployed-sha` is restored.
G7. **G.7** Notify: "Rollback to `<previous-rebuild-sha>`
   complete; root cause: see `cutover-report.md` § failure;
   pre-0.7 DB backup at `<backup-path>`; failed DB at
   `<DB_PATH>.failed-<ts>`."

If no previous rebuild wheel is available (this is the first
rebuild cutover), Phase G is moot; the only recovery is to debug
the new wheel against the backup-and-moved-aside pre-0.7 DB
(read-only, never modified).

## Stop point for this audit

Per the original spec: "complete the investigation, preservation,
clean branch creation, migration matrix, architecture, and test
plan. You may implement low-risk scaffolding and tests on
rebuild/upstream-0.7, but do not port large groups of custom commits
or modify production until the audit clearly identifies what is
required." Phases A + B are complete in this audit. **No production
was modified.**

## Genuine blockers that would prevent an in-place fresh-DB cutover

The Alembic head mismatch is **no longer a blocker**: a fresh
database does not need to be migrated from the fork lineage. The
remaining items are about *runtime configuration correctness* and
*operator workflow*.

### B-1. Existing systemd unit runs a non-`omni` binary (CANARY-LEVEL)

If the existing fork unit runs `python -m omnigent` from a clone
at `/srv/omnigent/repo`, the rebuild wheel installs a `omni` shim
to `~/.local/bin/omni` (via `uv tool install`). The new unit must
point at the shim, not at the clone. **Mitigation:** Phase C.4
explicitly rewrites `ExecStart=` to `/usr/local/bin/omni server …`.
Resolvable, no second install needed.

### B-2. Auth-header name mismatch (CANARY-LEVEL)

If the reverse proxy injects `Cf-Access-Authenticated-User-Email`
(or some other non-default header) and the systemd unit does
**not** set `OMNIGENT_AUTH_HEADER`, every request will 401 and
the phone URL will look broken even though the server is healthy.
**Mitigation:** Phase C.1 forces a single EnvironmentFile; verify
the header name against the proxy config before the cutover.
Resolvable in the unit file, no second install needed.

### B-3. Native-harness CLI version enforcement (CANARY-LEVEL)

Upstream PR #3335 (`92b1e10e`) added supported-CLI-version
enforcement for native harnesses. If the existing `pi`,
`opencode`, `claude`, `codex`, `cursor`, `kiro`, `goose`,
`hermes`, `qwen`, or `kimi` binaries on the production host
are outside the upstream-supported ranges, the new wheel will
refuse to launch them. **Mitigation:** capture the installed
versions during Phase D; if any are out of range, either upgrade
them in place or pin the supported range via the harness's
`OMNIGENT_<NAME>_VERSION_OVERRIDE` knob. Not a permanent blocker.

### B-4. Custom `--artifact-location` outside `<data_dir>/artifacts/` (CANARY-LEVEL)

If the existing fork unit passes `--artifact-location
/srv/omnigent/artifacts`, the upstream wheel honors it (verified
in `omnigent/cli.py:3192-3300` — `--artifact-location` is
preserved verbatim). **Mitigation:** keep the flag in the new
unit; verify the path is writable and that `/srv/omnigent/`
exists on the production host. Not a blocker.

### B-5. A running `--background` local server is already up (LOW RISK)

The upstream CLI's local-server pidfile machinery
(`~/.omnigent/local_server.pid`) is bypassed for `--host 0.0.0.0`
and for an explicit `--port`, so a real deploy never collides
with the daemon's canonical local server. But if the existing
fork unit happens to be running on `127.0.0.1` with the default
port *and* the daemon was also running on the same host, the
daemon will see a stale pidfile and try to reuse a dead PID.
**Mitigation:** the cutover script (Phase C.5) runs
`omnigent server stop` before `systemctl stop` to clean up.
Resolvable.

### B-6. Existing `chat.db` uses a custom SQLite cipher (NO LONGER BLOCKING)

Previously this was a blocker because we were going to migrate
the existing DB. Now that the existing DB is **moved aside
read-only and a fresh DB is initialized**, a SQLCipher-encrypted
legacy DB is no longer relevant to the new wheel — it is just
a read-only backup file. The new fresh DB is created by
`get_or_create_engine` + `_initialize_or_verify_schema` against
the empty path and is plain SQLite. **Mitigation:** none required
for the new wheel. The backup is preserved for forensic
inspection; reading it requires the SQLCipher key, which the
operator can apply manually if they wish to inspect it.

### B-7. `OMNIGENT_DATA_DIR` is not pinned (CANARY-LEVEL)

If the existing fork unit does not pin `OMNIGENT_DATA_DIR`, the
new wheel defaults to `~/.omnigent/` (e.g. `/root/.omnigent/`
when running as `root`, or `/home/<user>/.omnigent/` otherwise).
The cutover script's backup + move operations target a path
resolved from the EnvironmentFile. **Mitigation:** Phase C.1
pins `OMNIGENT_DATA_DIR=/var/lib/omnigent` explicitly. The
cutover script reads this from the EnvironmentFile before any
file operations. Resolvable.

## What is *not* a blocker (do not build for these)

- "What if we want to migrate the existing `chat.db` into the new
  wheel?" Answer: the user explicitly said the existing DB state
  is disposable. We are not doing this. No migration shim, no
  port of fork migrations, no Alembic head compatibility work.
  The old DB lives as a read-only backup.
- "What if we want A/B testing of the new wheel against the
  fork?" Answer: not a blocker. The Phase D disposable host
  is the canary. The phone URL is one user with one harness
  workflow; A/B testing is a marketing concept that does not
  map to single-user load.
- "What if we want a canary deployment that takes 5% of traffic?"
  Answer: same as above. The Phase D disposable host is the
  canary.
- "What if we want to preserve the old wheel for rollback?"
  Answer: the previous fork wheel is preserved as a historical
  artifact (in `legacy/custom-pre-0.7` + the dirty-index commit
  `1b8f15039c…`); the previous rebuild wheel is preserved as a
  rollback artifact at
  `/srv/omnigent/wheels/omnigent-<previous-rebuild-sha>.whl`
  (only relevant after the first rebuild cutover). Neither is
  a *second install* — neither is bound to a port, neither
  has a systemd unit.