# Phased implementation plan

Each phase is gated on evidence and does not begin until the previous
phase's evidence is recorded. **No phase modifies the production
installation**; production is touched only by Phase 8, after a clean
canary of the rebuild branch has been rehearsed, and even then only via
the production-promotion gate.

## Phase A — preservation (done)

A1. Verify repository state and produce `REPOSITORY_STATE_REPORT.md`.
A2. Create `legacy/custom-pre-0.7` (branch + tag) and
    `legacy/custom-pre-0.7-remote-main` (branch + tag) and
    `legacy/custom-pre-0.7-staged-20260802` (branch + tag for the dirty
    working-tree index).
A3. `git fetch --unshallow origin` (preserves `fetch_exit=0` log).
A4. Confirm the production fork and the new `rebuild/upstream-0.7`
    branch exist and that the upstream v0.7.0 tag SHA is recorded.

**Evidence committed to the rebuild branch:** the four documents
(`REPOSITORY_STATE_REPORT.md`, `MIGRATION_MATRIX.md`,
`ARCHITECTURE_PROPOSAL.md`, and the four planning documents) plus
this file.

## Phase B — minimal custom architecture (this audit only)

B1. Author the four planning documents (OmniRoute, Langfuse, harness
    Git, auto-update) and the acceptance test specification. **Done.**
B2. Confirm the architecture fits the upstream v0.7 file inventory
    (no missing APIs, every cross-reference resolves). **Done.**
B3. Validate the upstream-only claims against the actual v0.7 tree
    (read every file cited in the migration matrix; verify the
    upstream `omniroute` and `minimax` provider absence, the
    `prune_invalid_sub_agents` loader behavior, the
    `inject_trace_context` / `consume_frame_span` API, the
    `_SessionIdSpanProcessor` and `_redact_payload` helpers, the
    `ExternalRoutingClient` in `smart_routing.py`, the
    `OPENCODE_NATIVE_CODING_AGENT` declaration, the
    `process_manager.py:OMNIGENT_HARNESS_TMP_PARENT` env hook, the
    `routes/hosts.py` worktree request, the
    `host/git_worktree.py` worktree helpers, the
    `runtime/telemetry.py` opt-in, the `cli.py:upgrade` flow, the
    `RELEASING.md` and `omni-upgrade-design.md` docs). **Done.**

## Phase C — narrow core patches (upstream-quality PRs)

C1. **Liveness-aware watchdog** (upstream PR).
    * Re-implement `omnigent/runtime/harnesses/watchdog.py` on top of
      the v0.7 `_scaffold.py` (700 LOC + tests).
    * Add `SubprocessLivenessEvent` to `omnigent/server/schemas.py`.
    * Add the liveness loop + 4-budget watchdog to
      `omnigent/runtime/harnesses/_scaffold.py:_guarded_run_turn`.
    * Test: `tests/runtime/harnesses/test_watchdog_liveness.py` (522
      LOC, 27 tests).
    * Verify: `pytest tests/runtime/harnesses/` is green on v0.7 +
      the patch; `pytest tests/server/` is still green; the
      existing `HARNESS_TURN_TIMEOUT_S` env hook still works.
C2. **Identity-scoped `HostRegistry.deregister`** (upstream PR).
    * Add `deregister(connection: HostConnection | str) -> bool`.
    * Switch `omnigent/server/routes/host_tunnel.py` disconnect
      paths to the identity-scoped form.
    * Test: stale A cannot remove replacement B.
C3. **Optional: upstream PR** for the OpenCode canonical resolver if
    upstream's `OPENCODE_NATIVE_CODING_AGENT` + `prune_invalid_sub_agents`
    prove insufficient in the canary. (Gated on the canary output.)
C4. **Optional: upstream PR** for the structured OmniRoute provenance
    parser if the gateway surface becomes standardized.

## Phase D — Control-Room integration layer (sibling repo or `deploy/control-room/` subdir)

D1. Move the deployment/updater scripts out of `omnigent/`:
    * `scripts/{promote_release,rollback_release,write-dropin,_deployed_sha,deploy_status,cleanup_releases,install_omnigent_updater,omnigent_updater,promote_main_deploy,install_eval_*_unit}.sh`
    * `omnigent/deploy/ops/`, `omnigent/deploy/supervisor/`, `omnigent/deploy/preflight.py`
    * `omnigent/updater/`
    into `deploy/control-room/`.
D2. Re-implement the fork's deployment invariants as a thin
    control-room-only contract:
    * `deployed-sha` shared marker, atomic write helper.
    * Release-local artifact dir pinning.
    * Canary + health probe, exact previous-SHA rollback.
    * Host-pinning check using `/proc/<pid>/cmdline` (the fork's
      real bug; `/proc/<pid>/exe` resolves through uv's symlink
      target).
    * No `omnigent/` import anywhere in the control-room scripts
      (the scripts call `python` and `cli.py` as black-box processes).
D3. Author `deploy/control-room/omniroute/{provider.yaml,
    opencode_observer.py, provenance.py}` and
    `deploy/control-room/langfuse/{enable.env.example,
    deny-headers.txt, attributes.md}`.
D4. Author `deploy/control-room/harness/{pi,opencode}/git.md` and
    `worktree-conventions.md`.
D5. Author `deploy/control-room/acceptance/{contract.md, run.sh,
    conftest.py, fixtures/}` and the 15 pytest tests.

## Phase E — canary

E1. Build a clean `omnigent==0.7.0` wheel from the upstream `v0.7.0`
    tag.
E2. Run the 15 acceptance tests against the rebuild branch's
    Control-Room layer.
E3. Run a dry-run `promote_release.sh` against a fixture deploy root.
E4. Run a real `rollback_release.sh` after a fake promotion.
E5. Capture the canary results in `canary-report.md`; gate Phase F on
    "all 15 tests green; promote dry-run fails closed; rollback
    restores exact previous SHA".

## Phase F — production cutover

F1. Build a fresh `omnigent==0.7.0` wheel plus the rebuild branch's
    narrow core patches.
F2. Rehearse the migration on a disposable copy of the live
    production database; confirm `alembic upgrade head` is a clean
    no-op (or applies the minimal repair).
F3. Run the canary: install the wheel on a separate host, run a real
    session, confirm `deployed-sha` matches the release.
F4. Schedule a maintenance window; stop the existing host; run
    `promote_release.sh <sha>` for the rebuild release.
F5. Confirm `deployed-sha` points at the new SHA; the web service
    reports the new `version_sha`; the host service is active and
    pinned at the new release; the smoke test (a real user session
    that runs Pi + OpenCode + Verity) succeeds.
F6. Promote is gated on a human approval step; no unattended
    production promotion until the canary repeats successfully N
    times.

## Phase G — ongoing

G1. Quarterly rebase onto upstream `main` (or the next release tag).
G2. Watch upstream issues for materialization of any Matrix §6
    "unclear" item; resolve before each rebase.
G3. Periodically run the 15 acceptance tests against the new release.
G4. Re-evaluate the upstream contribution candidates (Phase C.1,
    C.2, C.3, C.4) on every minor release; backport accepted PRs.

## Stop point for this audit

Per the spec: "complete the investigation, preservation, clean branch
creation, migration matrix, architecture, and test plan. You may
implement low-risk scaffolding and tests on rebuild/upstream-0.7, but
do not port large groups of custom commits or modify production until
the audit clearly identifies what is required."

This audit's stop point is the end of Phase A + Phase B (preservation,
audit, planning, architecture) and the canary-target acceptance test
specification. Phase C (the two upstream-quality PRs) and Phase D
(Control-Room layer) are scheduled for the next task. **No production
was modified.**
