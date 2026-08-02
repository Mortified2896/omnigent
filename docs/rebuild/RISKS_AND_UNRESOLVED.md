# Risks and unresolved questions

## Risks (production-bound)

1. **Production database migration risk.** The live `chat.db` sits at
   `zg1a2b3c4d5e` (the fork's `zd1 → ze1 → zg1` chain). The
   rebuild branch's `rebuild/upstream-0.7` lineage starts at
   `zf1a2b3c4d5e` (upstream v0.7.0 head). Rehearsing the upgrade
   against a disposable copy of the live database is the only safe way
   to know the migration is a no-op; we **must not** assume it is. The
   fork's `omnigent/updater/migration_rehearsal.py` is the only
   mechanism that does this check; it lives in the Control-Room layer
   after the rebuild, not in upstream.

2. **Web UI lockfile divergence.** The fork's `bc22b799` "regenerate
   lockfile" commit is **already** in upstream v0.7 (the upstream
   `web/package-lock.json` includes `micromark-factory-mdx-expression`).
   The fork's `check_web_lockfile.sh` CI guard is still useful and
   should be re-implemented in the control-room scripts (or
   re-applied as an upstream PR if upstream has no equivalent gate).

3. **Direct provider fallback regression.** The fork's
   `minimax`/`omniroute` provider entries in
   `omnigent/llms/routing.py:PROVIDER_CONFIGS` recreate the same
   class of silent-fallback bug that the OpenCode canonical resolver
   prevents for the OpenCode harness. The rebuild branch **must
   not** carry these provider entries; the user-level `providers:`
   block in `~/.omnigent/config.yaml` is the correct location.

4. **Idle watchdog default.** The fork flipped the default idle
   watchdog off (`HARNESS_MODEL_STREAM_IDLE_S=0`,
   `HARNESS_TOOL_OUTPUT_IDLE_S=0`); upstream v0.7 keeps the default
   1-hour reap. The rebuild's port of the liveness-aware watchdog
   (§4.1 of the architecture proposal) makes the default off
   unnecessary in normal operation, but the deployment unit must
   continue to set `OMNIGENT_HARNESS_IDLE_TIMEOUT_S=0` if a
   particular session is expected to run quietly for hours. Document
   this in the Control-Room systemd unit.

5. **Database schema repair on the live DB.** The fork ships
   `zg1a2b3c4d5e_repair_conversations_kind.py` specifically because
   144 live `conversations` rows had drifted from the upstream
   schema. The rebuild's healthegrun rehearsal **must** apply this
   repair to a copy of the live database first; only then can the
   production cutover proceed.

6. **OpenCode harness self-dispatch is the spec's must-pass.** The
   spec explicitly forbids "silent fallback to Verity or claude
   SDK" for the OpenCode selector. Upstream v0.7's
   `OPENCODE_NATIVE_CODING_AGENT` + `prune_invalid_sub_agents` is
   the upstream mitigation; the fork's `agent_selector.py` is the
   extra-strict resolver. Acceptance test #6 verifies the chosen
   harness; a failure here is a hard block on the rebuild's go-live.

7. **Provenance columns in the live DB.** The fork adds
   `conversations.delegation_provenance` (migration `zf1a2b3c4d5e`)
   and the routing columns (migrations `z6a2b3c4d5e` and
   `za1b2c3d4e5f`). The rebuild branch's healthegrun rehearsal
   must confirm these columns are present on the live DB before
   production cutover; otherwise the new runtime's `INSERT`
   statements (which write to these columns) will fail.

8. **Updater controller domain overreach.** The fork's
   `omnigent/updater/` package is a deployment concern, not a
   runtime concern. The rebuild branch keeps upstream untouched
   here; the deployment layer re-implements the controller
   contract in `deploy/control-room/updater/`. Production cutover
   must use the new controller, not the old one, and the old one
   must be retired (its `/v1/updater/...` endpoints will return
   404 on the rebuild's runtime — that is by design).

9. **`rebuild/upstream-0.7` is not yet built or tested end-to-end.**
   The audit's canary (Phase E) is the next milestone. Until the
   canary is green, the rebuild branch is a planning artifact, not
   a production candidate. Do **not** schedule a maintenance
   window until Phase E is complete.

10. **Force-push of `rebuild/upstream-0.7` to `Mortified2896/omnigent`
    is intentionally blocked.** Only the audit-phase planning
    documents are pushed; the canary branch is local until
    Phase E.

## Risks (non-production)

11. **The fork's `omniroute_requires_explicit_approval` and
    `route_approval_enabled` session flags are not in upstream v0.7.**
    The Control-Room layer re-implements them as session-level
    labels; the canary verifies the wire format round-trips.

12. **The fork's `omniroute_*` migrations
    (`z6a2b3c4d5e_add_model_routing_fields`,
    `za1b2c3d4e5f_add_durable_routing_audit`,
    `z9a2b3c4d5e_separate_execution_evaluation_lifecycle`,
    `zb1b2c3d4e5f_persist_evaluation_lifecycle`,
    `zc1b2c3d4e5f_expand_review_action_with_not_logged`) are
    fork-only.** The upstream routing surface is sufficient for the
    rebuild's needs. If the production DB has any of these tables,
    they must be either migrated or dropped before the rebuild can
    read or write them; check with `alembic check` on a copy.

13. **The fork's `examples/control-room-polly/...` is replaced by
    upstream `examples/polly/...`.** No code change, just a
    documentation cross-link.

14. **The fork's `web/electron/signing/omnigent.provisionprofile` is
    a 12 KB binary** that does not exist in upstream. The rebuild
    branch should drop this file (it is fork-specific code-signing
    material). Verify with the team before deleting.

15. **The fork's `omnigent/llms/adapters/openai.py:49-58` adds
    `x-omniroute-*` header parsing** to the OpenAI adapter. This
    is OmniRoute-specific. The rebuild branch drops the change and
    re-implements the header parser in
    `deploy/control-room/omniroute/provenance.py`.

## Unresolved questions (need upstream or production-side answers)

1. **Does upstream v0.7's `routes/hosts.py` worktree request create
   a fresh worktree per session, or per sub-agent?** Verified: per
   session (the runner's `cd` cwd is set to the worktree path). Per
   sub-agent, the parent server's `POST /v1/sessions` for the child
   session uses the child's own worktree request, which the
   upstream code already supports via `SessionCreate.git`.

2. **Does upstream v0.7's `OpenCodeProvenanceProxy` exist in
   v0.7.0?** Verified: no. The proxy is a fork-only file. The
   Control-Room layer re-implements it outside `omnigent/`.

3. **Does upstream v0.7's `cli.py:upgrade` correctly handle
   `OMNIGENT_DATABASE_URI` / `OMNIGENT_ARTIFACT_LOCATION` env vars
   on the migration-step?** Verified: yes (per
   `tests/cli/test_upgrade_command.py`). The fork's
   `25f99e78` "fix(cli): honor OMNIGENT_DATABASE_URI / "
   work is already upstream.

4. **Does upstream v0.7's `routing-mvp` branch land into v0.7.0?**
   Verified: yes (the routing surface in v0.7.0's
   `smart_routing.py` includes `ExternalRoutingClient` and the
   combo-selection state). The fork's `routing_agent.py` is the
   obsolete competing implementation.

5. **Does the fork's `_DEFAULT_LINEAGE_ANCHOR = "3b42ccbe…"` still
   reference an ancestor of `rebuild/upstream-0.7`'s tip?**
   Verified: yes (`3b42ccbe` is an ancestor of v0.7.0? no — it
   is a fork-only commit; the rebuild branch's updater must use a
   new anchor against the v0.7.0 tag). The Control-Room updater
   defaults to `OMNIGENT_UPDATER_LINEAGE_ANCHOR=v0.7.0` (the tag
   SHA) on first run.

6. **Is the fork's `OMNIGENT_HARNESS_TMP_PARENT` canary override
   needed under upstream v0.7?** Verified: yes — the upstream
   `process_manager.py:67-78` already supports the env var, but the
   canary itself must be explicitly invoked by the deployment layer
   (it is not automatic on `omni upgrade`).

7. **Does the fork's `routing_agent.py:routes:select` integration
   still work on the v0.7.0 OmniRoute endpoint?** Untested. Phase
   E's canary must include a real `/routes:select` round-trip.

8. **Are the fork's `routing_proposals` and `routing_decisions`
   tables part of any production database?** Untested. The
   production-DB rehearsal must `SELECT count(*) FROM
   routing_proposals` on a copy.

9. **What is the size of the production `chat.db`?** Untested. The
   copy used for the migration rehearsal must be at least as large
   (so the rehearsal's `alembic upgrade head` exercises the same
   path lengths). If the live DB is > 1 GB, use a per-table copy
   for the rehearsal.

10. **Is the production host's `omni-control-room-host.service`
    unit file checked into the repository, or is it managed
    out-of-tree?** The fork's `install_eval_host_unit.sh` writes
    it; the rebuild's systemd unit template ships in
    `deploy/systemd/control-room/`. The live unit file is not
    checked in; the rebuild does not need to change it.

## Things to validate during the canary

- Real Pi session on the live harness binary; check that the
  watchdog survives a 10-minute quiet turn.
- Real OpenCode session; check the canonical resolver picks
  `opencode-native` even with a fuzzy request.
- Real Verity fan-out; check the per-task worktree isolation
  works for two concurrent children.
- Real `omni upgrade` from v0.7.0 to v0.8.0 (when released);
  check that the drain posture is correct and the
  control-room deployment tooling handles the cutover.
- Real `promote_release.sh` + `rollback_release.sh` cycle on a
  fixture deploy root; check the exact-SHA rollback and the
  cmdline-based host-pin check.
- Real Langfuse integration; check the trace hierarchy and the
  secret-redaction filter.
- Real OmniRoute combo selection; check that the
  `x-omniroute-*` headers are captured into the Langfuse trace.

## Open PRs the audit found but did not evaluate

- `Mortified2896/omnigent` PR #65 was abandoned (per PR #66's body).
- The `fork-e2e/pr-*` branches in upstream are mirror-only and
  not in scope.
- The 23 `backup/*` and 6 `staging-v060*` branches are historical
  forks; not in scope.
