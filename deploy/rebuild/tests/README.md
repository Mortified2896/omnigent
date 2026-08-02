# Disposable-host canary acceptance tests

The Phase D canary is a **disposable VM** (or local container) with
a fresh install of the rebuild's Omnigent 0.7 wheel, a fresh
empty database, and the rebuild's env.conf + server-config.yaml +
user-config.yaml + agent bundles wired in. **The canary never sees
the production database** — it uses a fresh empty SQLite file the
new wheel initializes itself on first boot.

The canary is only considered successful if it proves **all 12 of
the following**, in order:

1. Omnigent boots.
2. OmniRoute routes requests correctly.
3. Pi completes a real repository task.
4. Pi commits.
5. Pi pushes.
6. OpenCode completes a real repository task.
7. OpenCode commits.
8. OpenCode pushes.
9. Langfuse receives traces.
10. Verity orchestrates delegation correctly.
11. Worktrees remain isolated.
12. The same production URL and port configuration are used.

The goal is **not** to prove that Omnigent starts. The goal is to
prove that the rebuilt **Control Room workflow** is fully usable
*before* replacing production.

## How the canary runs

### Disposable host setup

```sh
# 1. Create a fresh VM or container. Debian 12 / Ubuntu 24.04 /
#    any Linux with systemd. The host has the rebuild wheel
#    installed via uv tool install --force (see Phase C.5).
# 2. Copy the canary fixtures into /opt/canary-fixtures/ on the
#    disposable host (see fixtures/ subdir).
# 3. Start the canary runner:
OMNIGENT_DATA_DIR=/var/lib/canary-omnigent \
    /opt/canary-fixtures/canary.sh run
```

`canary.sh run` performs the 12 checks in order. A failure on
check N halts the canary and exits non-zero; checks N+1..12 are
NOT attempted.

### Disposable DB

The canary's `OMNIGENT_DATA_DIR` is a fresh empty directory. The
rebuild wheel's first-boot migration path creates `chat.db` and
brings it to upstream v0.7 head (per
`omnigent/db/utils.py:_initialize_or_verify_schema`). The canary
does **NOT** touch the production `chat.db`.

### Disposable harness binaries

The canary disposable host has `pi` and `opencode` CLIs installed
under the system PATH (`/usr/local/bin/pi`,
`/usr/local/bin/opencode`). If a binary is missing, the
corresponding harness check (#3-#8) is SKIPPED with a clear log
message; checks #1, #2, #9, #10, #11, #12 still run.

## The 12 checks

Each check is a single pytest-style function or shell command that
asserts the expected outcome. The full check implementations are
under `checks/` in this directory; each one returns a structured
`{name, status, evidence}` dict the canary runner aggregates.

### Check 1 — Omnigent boots

`checks/01_boot.py` (or `01_boot.sh`).

- Boot the new wheel against an empty `OMNIGENT_DATA_DIR`.
- Wait up to 45 s for `/health` to return 200.
- Assert `journalctl -u omnigent -n 50` contains
  `"Running database migrations"` and does NOT contain
  `"schema is out of date"`.
- Assert `sqlite3 <data_dir>/chat.db ".tables"` returns at
  least: `alembic_version`, `agents`, `conversations`,
  `comments`, `files`, `policies`, `permissions`,
  `scheduled_tasks`, `projects`, `inbox`, `tasks`.
- Assert `sqlite3 <data_dir>/chat.db "SELECT version_num
  FROM alembic_version"` returns the upstream v0.7 head SHA
  (`zf1a2b3c4d5e`).

**Pass criteria**: every assertion above passes; `/health` returns
200 inside 45 s.

### Check 2 — OmniRoute routes requests correctly

`checks/02_omniroute.py`.

- A real `/v1/route` POST against the deployed OmniRoute
  gateway. The request body is a minimal "trivial echo" prompt
  with `available_models` containing the provider's known
  catalog.
- Assert the response contains a non-empty `provider`, `model`,
  and `decision_id`.
- Assert the response carries the canonical OmniRoute response
  headers (`x-omniroute-request-id`,
  `x-omniroute-decision-id`, `x-omniroute-selected-provider`,
  `x-omniroute-selected-model`,
  `x-omniroute-requested-model`).
- Assert a follow-up `/v1/route` POST with the SAME prompt and
  DIFFERENT catalog produces a deterministic response (idempotent
  or at least cacheable within a single decision_id).

**Pass criteria**: both assertions above pass; the gateway
returned a real provider+model pairing with all expected headers.

### Check 3 — Pi completes a real repository task

`checks/03_pi_repo_edit.sh`.

- Create a disposable git fixture repo at
  `/tmp/canary-fixtures/<run-id>/pi/repo` with a single file
  `module.py` containing `def foo(): return 1`.
- Boot the new wheel (still alive from check 1).
- `POST /v1/sessions` with `agent_selector: "pi"`,
  `workspace: /tmp/canary-fixtures/<run-id>/pi/worktree`,
  `prompt: "rename the function foo to bar"`.
- Wait up to 180 s for `session.finished`.
- Assert `git -C <worktree> diff main` contains `bar` and does
  NOT contain `foo`.

**Pass criteria**: the diff shows `bar`, not `foo`, and the
session completed inside 180 s. (Or SKIPPED if `pi` binary is
missing from PATH.)

### Check 4 — Pi commits

`checks/04_pi_commit.sh` (depends on check 3).

- The Pi session from check 3 must produce at least one
  `git.commit.outcome` SSE event with a non-empty `commit_sha`.
- `git -C <worktree> log --oneline` shows a commit authored by
  the harness identity (any email; the harness's identity is
  config-driven, not asserted here).

**Pass criteria**: the session emitted `git.commit.outcome` with
a non-empty SHA, and `git log` shows the new commit.

### Check 5 — Pi pushes

`checks/05_pi_push.sh` (depends on check 4).

- The Pi session's commit must be pushed to a branch on a
  disposable remote (the canary fixture's bare repo at
  `/tmp/canary-fixtures/<run-id>/pi/remote.git`).
- Assert `git -C <remote-worktree> rev-parse
  polly/acceptance-5-pi` returns the SHA from the
  `git.commit.outcome` event.

**Pass criteria**: the pushed SHA on the remote matches the
session's reported commit SHA.

### Check 6 — OpenCode completes a real repository task

`checks/06_opencode_repo_edit.sh`.

- Symmetric to check 3, but with `agent_selector: "opencode"`.
- Assert `git -C <worktree> diff main` contains `bar` and does
  NOT contain `foo`.

**Pass criteria**: same as check 3. (Or SKIPPED if `opencode`
binary is missing from PATH.)

### Check 7 — OpenCode commits

`checks/07_opencode_commit.sh`.

- Symmetric to check 4. Asserts the
  `git.commit.outcome` SSE event and the resulting `git log`
  entry.

### Check 8 — OpenCode pushes

`checks/08_opencode_push.sh`.

- Symmetric to check 5. Asserts the remote's
  `polly/acceptance-8-opencode` branch carries the session's
  reported SHA.

### Check 9 — Langfuse receives traces

`checks/09_langfuse.py`.

- Boot the new wheel with
  `OMNIGENT_TELEMETRY_ENABLED=1` + the configured
  `OTEL_EXPORTER_OTLP_ENDPOINT` pointing at a real Langfuse
  sandbox (the canary does NOT mock this).
- Run a single trivial session (a `claude-sdk` session with
  `prompt: "trivial echo"`).
- Poll Langfuse's public API
  (`/api/public/sessions/<session_id>`) for up to 30 s for the
  emitted trace.
- Assert the trace hierarchy contains:
    - one root span with `session.id == <session_id>`,
    - one or more child spans (e.g. `omnigent.tool`,
      `omnigent.llm.request`) each carrying the same
      `session.id`,
    - the OmniRoute provenance attributes from check 2 (when
      OmniRoute was used).

**Pass criteria**: a real trace appeared in Langfuse with the
expected hierarchy and attributes within 30 s of session
completion.

### Check 10 — Verity orchestrates delegation correctly

`checks/10_verity_delegation.sh`.

- Boot the new wheel with the rebuild's three agent bundles
  (`verity`, `pi`, `opencode`) registered via `--agent` on the
  systemd unit.
- `POST /v1/sessions` with `agent_selector: "verity"`,
  `prompt: "rename foo to bar across the test repo using a
  Pi sub-agent"`.
- Wait up to 240 s for `session.finished`.
- Assert the session log contains:
    - one `sys_session_send` tool call with `args.purpose: "implement"`,
    - one child `session.created` event with
      `agent_selector: "pi"`,
    - one `session.harness` event with
      `payload.harness == "pi-native"`,
    - one `session.child_session.updated` event in the parent's
      stream,
    - one `sub_agent` item in the parent's `sys_read_inbox`
      when the child finished.
- Assert the worktree directory recorded by the parent session
  for the child differs from the parent's worktree (per-session
  worktree isolation; see check 11).

**Pass criteria**: every assertion above passes; the
parent's stream shows the full delegation lifecycle.

### Check 11 — Worktrees remain isolated

`checks/11_worktree_isolation.sh`.

- Boot the new wheel.
- Run two parallel Pi sessions in parallel (two
  `POST /v1/sessions` with `agent_selector: "pi"` against the
  same disposable fixture repo, both `prompt: "rename foo to
  <something>"`).
- Wait up to 240 s for both to finish.
- Assert each session created a distinct worktree directory
  under `<data_dir>/worktrees/<session_id>-<n>/`.
- Assert the two sessions produced commits on distinct branches
  (e.g. `polly/acceptance-11-pi-a` and
  `polly/acceptance-11-pi-b`).
- Assert the two commits are byte-distinct (different SHAs).
- Assert no session ever wrote outside its own worktree
  (filesystem snapshot of `<data_dir>/worktrees/<other-session>/`
  is unchanged before / after the parallel run).

**Pass criteria**: every assertion above passes; no session
clobbered another's worktree.

### Check 12 — Same production URL and port configuration are used

`checks/12_prod_url.py`.

- Boot the new wheel with the SAME `--host 0.0.0.0` and the
  SAME `--port <PROD_PORT>` the production deploy uses.
- Assert `curl -fsS http://127.0.0.1:<PROD_PORT>/health`
  returns 200.
- Assert the `OMNIGENT_AUTH_HEADER` env var matches what the
  production reverse proxy injects (the canary reads this from
  `env.conf` and asserts the value matches the operator's
  pre-cutover note).
- Assert the `OMNIGENT_DATA_DIR` env var points at
  `/var/lib/omnigent` (the same path the production deploy
  uses; the canary's `<data_dir>/deployed-sha` is the canary's
  own marker, not the production's, but the env var must
  match).
- Assert the systemd unit's `ExecStart=` matches the
  canonical template the Phase C.4 unit template renders, with
  `<OMNI_BIN_PATH>` substituted for the actual installed
  shim's absolute path.
- Assert the rebuild's `deployed-sha` marker (if present)
  carries the rebuild release SHA, not the fork's pre-cutover
  SHA.

**Pass criteria**: every assertion above passes; the canary
unit is configurationally identical to the production unit
except for the data dir and the deployed-sha marker.

## Output: `canary-report.md`

Each successful canary run writes a structured report to
`docs/rebuild/canary-report.md` on the rebuild branch (via the
`canary-publish.sh` script the canary runner calls on success).
The report includes:

- run timestamp, canary host fingerprint
- per-check status (PASS / FAIL / SKIPPED) + duration
- per-check evidence (the assertion outputs, the journalctl
  excerpts, the Langfuse trace ID, the Pi/OpenCode commit
  SHAs, etc.)
- summary table at the top: 12 rows, one per check, color-coded

A canary run is considered "all green" only when all 12 checks
PASS or are explicitly SKIPPED (a SKIPPED check must be one of
the harness-binary-missing checks: #3, #4, #5, #6, #7, #8). A
single FAIL halts the canary and the report is written with the
failure highlighted at the top.

## What the canary does NOT do

- It does NOT touch the production database. The canary uses
  a fresh empty SQLite file the new wheel creates itself.
- It does NOT touch the production release dir, the production
  systemd unit, or the production reverse-proxy config.
- It does NOT install a wheel on the production host.
- It does NOT open PRs against the production repo (the
  disposable remote is a bare git repo inside the canary
  fixture, NOT `Mortified2896/omnigent`).

## Next step after a green canary

Move to **Phase E** — the production cutover. The script is
`scripts/cutover.sh` (committed in Phase C.5), invoked with
`--confirm --rebuild-sha <canary-passing-sha> --port <PROD_PORT>
--user <service-user>`. The cutover script's output is the
"Phase E" evidence captured in
`docs/rebuild/cutover-report.md`.