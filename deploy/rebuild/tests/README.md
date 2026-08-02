# Pre-cutover canary acceptance tests (Phase D)

Phase D is the **pre-cutover canary**. It runs the rebuild's Omnigent
0.7 wheel on the **existing Omnigent VM** (the same host that
already runs production), NOT on a disposable VM and NOT on
another host. The existing VM already contains the real environment
the canary must validate (installed Pi / OpenCode harnesses, Git +
GitHub credentials, OmniRoute connectivity, Langfuse connectivity,
filesystem paths and permissions, systemd, the reverse-proxy /
network environment, production-equivalent runtime dependencies).

Isolation from the running production stack is enforced by
**pointing the rebuild wheel at temporary resources**: an unused
loopback TCP port, a temporary `OMNIGENT_DATA_DIR`, a fresh
temporary SQLite database (created by the wheel itself on first
boot), a temporary artifact directory, a temporary harness tmp
directory, temporary copies of the rebuild's env.conf +
server-config.yaml + user-config.yaml + agent bundles, and
disposable Git branches (or a dedicated disposable GitHub test
repo). The canary does **NOT** stop or restart the current
production Omnigent service, does **NOT** modify the live
`omnigent.service` unit, does **NOT** bind the current production
port, does **NOT** touch, move, copy, migrate, or modify the
live `chat.db`, does **NOT** alter the reverse proxy, does
**NOT** install the rebuild wheel over the current production
installation, and does **NOT** perform the Phase E cutover.

The 12 acceptance checks are:

1. Omnigent boots (fresh 0.7 database initialization on the
   existing VM, against the temp `OMNIGENT_DATA_DIR`).
2. Web/API health on the **temporary** loopback port (NOT the
   production port).
3. **A real OmniRoute-routed request with requested and executed
   model provenance** (the response carries `provider`, `model`,
   `decision_id`, and the canonical `x-omniroute-*` response
   headers including the requested and selected model).
4. Pi edits a disposable repository.
5. Pi commits the change.
6. Pi pushes the branch.
7. OpenCode edits a disposable repository.
8. OpenCode commits the change.
9. OpenCode pushes the branch.
10. Langfuse receives the trace and it is verified from Langfuse
    (real OTLP export, real Langfuse tenant, real `session.id` +
    child-span inspection from Langfuse's API).
11. Verity delegates to the intended harness **without silent
    fallback** (no fallback to `claude-sdk` or to the default
    agent; the requested harness selector is honored end-to-end).
12. Parallel workers remain isolated in separate worktrees
    (two parallel Pi sessions in the same canary run produce
    distinct worktrees, distinct branches, distinct
    byte-distinct SHAs, and no cross-worktree filesystem
    contamination).

The goal is **not** to prove that Omnigent starts. The goal is to
prove that the rebuilt **Control Room workflow** is fully usable
*before* replacing production.

## How the canary runs

### Setup on the existing Omnigent VM

The canary runs **on the existing Omnigent VM**. No new VM is
provisioned.

```sh
# On the existing VM (as the operator) — preparation:
#
# 1. Copy the rebuild's deployment fixtures into a temp prefix:
#    /etc/omnigent-canary-<run-id>/   (env.conf)
#    /var/lib/omnigent-canary-<run-id>/config.yaml
#    /srv/omnigent-canary-<run-id>/agents/
#    /srv/omnigent-canary-<run-id>/artifacts/
#    /srv/omnigent-canary-<run-id>/harness-tmp/
# 2. Install the rebuild wheel to a temp-prefixed bin dir
#    (uv tool install --force --bin-dir /opt/canary-bin
#     /srv/omnigent/wheels/omnigent-0.7.0+rebuild-<sha>.whl).
# 3. Pick an UNUSED TCP loopback port, e.g. 17670.
# 4. Start the canary runner with the temp resources:
ENV_FILE=/etc/omnigent-canary-<run-id>/env.conf \
OMNIGENT_DATA_DIR=/var/lib/omnigent-canary-<run-id> \
OMNIGENT_PORT=17670 \
OMNIGENT_AUTH_HEADER=X-Forwarded-Email \
CANARY_IDENTITY=canary@omnigent.local \
CANARY_FIXTURES_ROOT=/tmp/canary-fixtures \
/opt/canary/rebuild-canary.sh run --rebuild-sha <sha>
```

The canary executes as either a **temporary foreground process**
under `bash` (the default) or, if systemd-level service behaviour
must be validated, an **isolated temporary systemd unit** at
`/etc/systemd/system/omnigent-canary.service` (clearly separate
name) that is removed at the end of Phase D. **It must not
replace or modify `omnigent.service`.**

`canary.sh run` performs the 12 checks in order. The runner
records every check's status; a failure on check N does not
halt the runner — the runner continues to attempt the
remaining checks so the operator gets a complete picture of
which checks PASS, FAIL, or SKIPPED. The runner exits 0
only when all 12 checks PASS (and no Pi/OpenCode check is
SKIPPED); a single FAIL or required SKIPPED exits non-zero.

### Fresh temp DB

The canary's `OMNIGENT_DATA_DIR` is a fresh empty directory. The
rebuild wheel's first-boot migration path creates `chat.db` and
brings it to upstream v0.7 head (per
`omnigent/db/utils.py:_initialize_or_verify_schema`). The canary
does **NOT** touch the production `chat.db`; the production
`OMNIGENT_DATA_DIR` is never read by the canary process.

### Harness binaries

The existing Omnigent VM must have `pi` and `opencode` CLIs
installed under the system PATH (`/usr/local/bin/pi`,
`/usr/local/bin/opencode`). If a binary is missing, the
corresponding harness check (#4–#9) is SKIPPED with a clear log
message, and **a run containing such a SKIPPED is NOT green** —
Pi and OpenCode checks are mandatory for Phase E authorization.
SKIPPED may appear only while diagnosing initial harness setup;
once the binaries are installed, every Pi and OpenCode check
must report PASS.

## The 12 checks

Each check is one of: a shell script (POSIX `sh`) under
`checks/`, or a python3 (stdlib only) script under `checks/`. Each
script prints a structured status line on stdout (`PASS`, `FAIL
<reason>`, or `SKIPPED (harness binary missing)`) and the runner
aggregates them into `canary-report.md`.

The per-check script filenames under `deploy/rebuild/tests/checks/`
are numbered to match the user's 12-check specification
(01 = DB init, 02 = health on the temp port, 03 = OmniRoute,
04–06 = Pi, 07–09 = OpenCode, 10 = Langfuse, 11 = Verity, 12 =
parallel-isolation). The launcher `canary.sh` runs them in this
order and aggregates per-check outcomes into
`docs/rebuild/canary-report.md`. Each check writes a structured
status line on stdout (`PASS`, `FAIL <reason>`, or `SKIPPED
(harness binary missing)`) and a structured evidence block
the runner appends to the per-run log under
`docs/rebuild/canary-runs/<run-id>/log/<check>.log`. The
descriptions below are the spec.

### Check 1 — Fresh Omnigent 0.7 database initialization

- Boot the rebuild wheel against the temp `OMNIGENT_DATA_DIR`
  (an empty directory on the existing Omnigent VM; NOT the
  production `/var/lib/omnigent`, NOT a disposable VM).
- Wait up to 45 s for the first-boot migration path to fire
  (`Running database migrations…` log line on stdout / journalctl).
- Assert no `schema is out of date` error.
- Assert `sqlite3 <temp_data_dir>/chat.db ".tables"` returns at
  least: `alembic_version`, `agents`, `conversations`,
  `comments`, `files`, `policies`, `permissions`,
  `scheduled_tasks`, `projects`, `inbox`, `tasks`.
- Assert `sqlite3 <temp_data_dir>/chat.db "SELECT version_num
  FROM alembic_version"` returns the upstream v0.7 head SHA
  (`zf1a2b3c4d5e`).

**Pass criteria**: every assertion above passes; the fresh
schema is at upstream v0.7 head.

### Check 2 — Web/API health on the temporary port

- The rebuild wheel is bound to an **unused temporary loopback
  TCP port** (e.g. `127.0.0.1:17670`), NOT to the production
  port (e.g. NOT `0.0.0.0:6767`).
- Assert `curl -fsS http://127.0.0.1:<canary-port>/health`
  returns 200 inside 10 s.
- Assert the wheel is reachable on the canary port from the
  reverse-proxy network namespace equivalent
  (e.g. `curl --unix-socket` if so configured, or a
  same-network curl with the auth header injected).

**Pass criteria**: `/health` returns 200 on the temp port; the
production port is not bound by the canary wheel.

### Check 3 — A real OmniRoute-routed request with requested and executed model provenance

- A real `POST /v1/route` (or `POST <omniroute>/routes:select`)
  against the deployed OmniRoute gateway. The request body is a
  minimal prompt with `available_models` containing the
  provider's known catalog.
- Assert the response contains a non-empty `provider`, `model`,
  and `decision_id`.
- Assert the response carries the canonical OmniRoute response
  headers including **both** the requested model
  (`x-omniroute-requested-model`) **and** the executed model
  (`x-omniroute-selected-model` / `x-omniroute-selected-provider`).
- Assert a follow-up POST with the same prompt + same catalog
  returns the same `decision_id` (determinism).

**Pass criteria**: a real provider+model pairing was returned;
both requested and executed model provenance are present; the
gateway is deterministic for the same input.

### Check 4 — Pi edits a disposable repository

- A disposable git fixture repo at
  `/tmp/canary-fixtures/<run-id>/pi/repo` with a single file
  `module.py` containing `def foo(): return 1`.
- `POST /v1/sessions` with `agent_selector: "pi"`,
  `workspace: /tmp/canary-fixtures/<run-id>/pi/worktree`,
  `prompt: "rename the function foo to bar"`.
- Wait up to 240 s for the session to complete. The v0.7 session
  status has no `finished` value, and native-TUI harnesses return
  the session to `idle` as soon as the turn is dispatched (the TUI
  then works asynchronously) — the helper waits for the worktree
  HEAD to advance past the fixture's `main` instead.
- Assert the worktree's `module.py` contains `def bar()` and does
  NOT contain `def foo()`. (A rename appears in `git diff main` as
  `-def foo():` / `+def bar():`, so the diff itself always contains
  `foo` and cannot be the assertion target.)

**Pass criteria**: the worktree's `module.py` shows `def bar()`,
not `def foo()`, and the session completed inside 240 s. (SKIPPED
only while diagnosing an absent `pi` binary on the VM; once `pi`
is installed, this check must report PASS, not SKIPPED.)

### Check 5 — Pi commits the change

- The Pi session's worktree HEAD must be a new commit beyond the
  fixture's `main` (read from the worktree directly — the v0.7
  session event surface has no `git.commit.outcome` event).
- `git -C <worktree> log --oneline` shows a commit authored by
  the harness identity (any email; the harness's identity is
  config-driven, not asserted here).

**Pass criteria**: the worktree HEAD is a new commit with a
non-empty SHA, and `git log` shows it.

### Check 6 — Pi pushes the branch

- The Pi session's commit must be pushed to a branch on a
  disposable remote (the canary fixture's bare repo at
  `/tmp/canary-fixtures/<run-id>/pi/remote.git`, OR a dedicated
  disposable GitHub test repo).
- Assert the remote's `polly/canary-6-pi-<run-id>` branch tip
  equals the worktree HEAD SHA.

**Pass criteria**: the pushed SHA matches the session's
reported commit SHA.

### Check 7 — OpenCode edits a disposable repository

- Symmetric to check 4, but with `agent_selector: "opencode"`
  on a fresh fixture repo at
  `/tmp/canary-fixtures/<run-id>/opencode/repo`.

**Pass criteria**: same as check 4. (SKIPPED only while
diagnosing an absent `opencode` binary.)

### Check 8 — OpenCode commits the change

- Symmetric to check 5.

### Check 9 — OpenCode pushes the branch

- Symmetric to check 6; branch name `polly/canary-9-opencode-<run-id>`.

### Check 10 — Langfuse receives the trace and it is verified from Langfuse

- The rebuild wheel is booted with `OMNIGENT_TELEMETRY_ENABLED=1`
  + the configured `OTEL_EXPORTER_OTLP_ENDPOINT` pointing at a
  real Langfuse tenant (the canary does NOT mock this).
- Run a single trivial session.
- Poll Langfuse's public API for up to 30 s for the trace.
- Verify the trace hierarchy contains a `session.id` matching
  the session, child spans for `omnigent.tool` /
  `omnigent.llm.request`, and (when OmniRoute was used) the
  OmniRoute provenance attributes from check 3.

**Pass criteria**: a real trace appeared in Langfuse with the
expected hierarchy and attributes within 30 s of session
completion; the verification is read from Langfuse's API, not
assumed.

### Check 11 — Verity delegates to the intended harness without silent fallback

- The rebuild's three agent bundles (`verity`, `pi`, `opencode`)
  are registered via `--agent` (or a temp config file +
  `--agent`).
- `POST /v1/sessions` with `agent_selector: "verity"`,
  `prompt: "rename foo to bar using a Pi sub-agent"`.
- Assert the parent's session log contains the lifecycle:
  `sys_session_send{purpose: "implement"}` → a child
  `session.created` with the requested selector → a
  `session.harness` event with `payload.harness` matching the
  requested harness (NOT silently falling back to
  `claude-sdk` or to the default agent).
- Assert a `session.child_session.updated` event and a
  `sub_agent` item in the parent's `sys_read_inbox`.

**Pass criteria**: the requested harness name is honored
end-to-end; no silent fallback to `claude-sdk` or to the
default agent.

### Check 12 — Parallel workers remain isolated in separate worktrees

- Run two parallel Pi sessions against the same disposable
  fixture repo (both `prompt: "rename foo to <something>"`).
- Wait up to 240 s for both to finish.
- Assert each session has a distinct worktree directory under
  the temp data dir's `worktrees/`.
- Assert the two sessions produced byte-distinct commits on
  distinct branches.
- Assert no session wrote outside its own worktree
  (filesystem snapshot hash unchanged).

**Pass criteria**: every assertion above passes; no session
clobbered another's worktree.

## Acceptance rule (Phase E gate)

Phase E (the production cutover) is authorized only when:

- Phase D.1: 12/12 PASS.
- Phase D.2 (repeatability run with the same temp
  configuration, against a freshly emptied temp
  `OMNIGENT_DATA_DIR`): 12/12 PASS.
- No FAIL anywhere in either `docs/rebuild/canary-report.md`.

**Pi and OpenCode checks (#4–#9) are mandatory**; a SKIPPED
for any of them does not authorize Phase E. SKIPPED may
appear **only while diagnosing setup** (e.g. a missing `pi`
or `opencode` binary during initial bring-up); once the
harness binaries are installed, every Pi and OpenCode check
must report PASS, not SKIPPED. A run containing a required
SKIPPED is treated as a FAIL for purposes of authorizing
Phase E.

## Output: per-run report + durable run directory

Each canary run writes:

- A structured report to `docs/rebuild/canary-runs/<run-id>/canary-report.md`
  (the durable, per-run artifact).
- A mirror at `docs/rebuild/canary-report.md` (the latest D.1
  run) or `docs/rebuild/canary-report-repeat.md` (the latest
  D.2 run).
- A per-run env snapshot at
  `docs/rebuild/canary-runs/<run-id>/canary.env`.
- A per-check log under
  `docs/rebuild/canary-runs/<run-id>/log/<check>.log`
  (full stdout + stderr + parsed status + duration + evidence).
- A TSV summary at
  `docs/rebuild/canary-runs/<run-id>/results.tsv`.

Each report includes:

- run timestamp, canary host fingerprint (whoami + uname +
  ip route, NOT a destructive fingerprint)
- the rebuild SHA the canary is exercising
- the canary port, the production port, the data dir, and
  the auth header
- per-check status (PASS / FAIL / SKIPPED) + duration
- per-check evidence (the assertion outputs, the journalctl
  excerpts, the Langfuse trace ID, the Pi/OpenCode commit
  SHAs, etc.)
- summary table at the top: 12 rows, one per check, with
  per-row PASS/FAIL/SKIPPED + duration.

A canary run is considered "all green" only when **all 12
checks PASS or the SKIPPED are limited to harness-binary
diagnostic runs**. A single FAIL halts the canary and the
report is written with the failure highlighted at the top.

## Phase D.2 — repeatability run (separate evidence)

The canary runs twice: **D.1** is the initial canary against
a fresh empty temp data dir; **D.2** is the repeatability
run against the **same temp configuration** (env.conf,
configs, agent bundles) but a freshly-emptied temp data
dir. The runner distinguishes the two:

```sh
# D.1 — fresh canary against a NEW temp data dir.
canary.sh run --rebuild-sha <sha> --run-id 20260802T091500Z

# D.2 — same temp config, freshly emptied data dir.
canary.sh repeat --rebuild-sha <sha> --run-id 20260802T091500Z
```

`repeat` wipes `OMNIGENT_DATA_DIR` before the run (the
operator can opt out with `--reuse-data-dir`) and writes its
report to `docs/rebuild/canary-runs/<run-id>-repeat/`
(symmetric to the D.1 layout). The mirrored top-level
report is `docs/rebuild/canary-report-repeat.md` (D.1's
top-level mirror is `docs/rebuild/canary-report.md`).

**The D.1 and D.2 reports are kept separate so the operator
can diff them** (Phase E is authorized only when both
reports show 12/12 PASS). The runner enforces the same
green/red rules for both:

- A Pi/OpenCode check (#4–#9) reporting `SKIPPED` is treated
  as a FAIL for the purposes of authorizing Phase E.
- A `FAIL` halts the run; remaining checks are not
  attempted.
- A red D.2 run halts the authorization gate even if D.1
  was green.

## What the canary does NOT do

- It does NOT touch the production database. The canary uses
  a fresh empty SQLite file the new wheel creates itself,
  inside the temp `OMNIGENT_DATA_DIR` the canary started
  with.
- It does NOT modify the live `omnigent.service` unit; if a
  temp unit is needed for service-behaviour validation, it is
  installed at `omnigent-canary.service` and removed at the
  end of Phase D.
- It does NOT bind the production port; the canary binds an
  unused temp loopback port.
- It does NOT alter the reverse proxy, the env vars the
  production service reads, or anything else the running
  production service depends on.
- It does NOT install the rebuild wheel over the current
  production installation; the rebuild wheel is installed to
  a temp-prefixed bin dir (e.g. `uv tool install --force
  --bin-dir /opt/canary-bin`) and referenced from there.
- It does NOT perform the Phase E cutover.

## Next step after a green canary (Phase D + repeatability run)

Move to **Phase E** — the production cutover. **Phase E is the
only in-place production operation in this plan**: stop the
existing service, preserve and move aside the old database,
install the rebuilt wheel, start the new version on the
**existing production port**, keep the same hostname and
phone URL. The script is `scripts/cutover.sh` (committed in
Phase C.5), invoked with `--confirm --rebuild-sha
<canary-passing-sha> --port <PROD_PORT> --user <service-user>`.
The cutover script's output is the "Phase E" evidence captured
in `docs/rebuild/cutover-report.md`.