# Risks and unresolved questions

## Risks (production-bound)

1. **Legacy `chat.db` becomes unreadable after cutover.** The
   existing production `chat.db` (fork lineage, head `zg1a2b3c4d5e`)
   is moved aside to a read-only, immutable backup. It is preserved
   **for forensic inspection**, not for restore. Anyone who needs
   to read the legacy data after the cutover must do so manually
   (e.g. `sqlite3 chat.db.pre-0.7.<ts> ".dump"` or
   `sqlite3 chat.db.pre-0.7.<ts> ".schema"`). **The cutover
   script must verify the backup's SHA-256, non-empty size, and
   SQLite schema before moving the live file aside.**

2. **Backup file loss / corruption.** The cutover writes the
   backup to `/var/lib/omnigent/backups/chat.db.<UTC-ts>.bak`,
   then `chmod 0444` + `chattr +i`. If the host's `/var/lib/omnigent`
   is on a separate volume that is not snapshotted, the backup is
   at the same risk as the live file. **Mitigation:** the cutover
   script logs the backup's path, size, and SHA-256 to
   `cutover-report.md`; if `/var/lib/omnigent/backups/` is on a
   separate volume, the script copies the backup to a second
   location (e.g. `s3://<bucket>/omnigent-pre-0.7/<UTC-ts>.bak`)
   before `chattr +i`.

3. **Auth-header name mismatch.** If the reverse proxy injects
   `Cf-Access-Authenticated-User-Email` (Cloudflare Access) or
   another non-default header and the systemd unit does **not**
   set `OMNIGENT_AUTH_HEADER`, every request will 401 and the
   phone URL will look broken even though the server is healthy.
   **Mitigation:** Phase C.1 forces a single EnvironmentFile;
   verify the header name against the proxy config before the
   cutover. Resolvable in the unit file, no second install
   needed.

4. **Web UI lockfile divergence.** The fork's `bc22b799`
   "regenerate lockfile" commit is **already** in upstream v0.7
   (the upstream `web/package-lock.json` includes
   `micromark-factory-mdx-expression`). The fork's
   `check_web_lockfile.sh` CI guard is still useful and should
   be re-implemented in the control-room scripts (or re-applied
   as an upstream PR if upstream has no equivalent gate).

5. **Direct provider fallback regression.** The fork's
   `minimax`/`omniroute` provider entries in
   `omnigent/llms/routing.py:PROVIDER_CONFIGS` recreate the same
   class of silent-fallback bug that the OpenCode canonical
   resolver prevents for the OpenCode harness. The rebuild
   branch **must not** carry these provider entries; the
   user-level `providers:` block in `/var/lib/omnigent/config.yaml`
   is the correct location.

6. **Idle watchdog default.** The fork flipped the default idle
   watchdog off (`HARNESS_MODEL_STREAM_IDLE_S=0`,
   `HARNESS_TOOL_OUTPUT_IDLE_S=0`); upstream v0.7 keeps the
   default 1-hour reap. The rebuild's port of the liveness-aware
   watchdog (§4.1 of the architecture proposal) makes the default
   off unnecessary in normal operation, but the deployment unit
   must continue to set `OMNIGENT_HARNESS_IDLE_TIMEOUT_S=0` if a
   particular session is expected to run quietly for hours.
   Document this in the Control-Room systemd unit.

7. **OpenCode harness self-dispatch is the spec's must-pass.**
   The spec explicitly forbids "silent fallback to Verity or
   claude SDK" for the OpenCode selector. Upstream v0.7's
   `OPENCODE_NATIVE_CODING_AGENT` + `prune_invalid_sub_agents`
   is the upstream mitigation; the fork's `agent_selector.py`
   is the extra-strict resolver. Acceptance test #8 verifies
   the chosen harness; a failure here is a hard block on the
   rebuild's go-live.

8. **Native-harness CLI version enforcement.** Upstream PR #3335
   (`92b1e10e`) added supported-CLI-version enforcement for
   native harnesses. If the existing `pi`, `opencode`, `claude`,
   `codex`, `cursor`, `kiro`, `goose`, `hermes`, `qwen`, or
   `kimi` binaries on the production host are outside the
   upstream-supported ranges, the new wheel will refuse to launch
   them. **Mitigation:** capture the installed versions on the
   existing Omnigent VM during Phase D (the canary runs on the
   existing VM, so the host's installed binary versions are
   exactly the ones the production cutover will see); if any are
   out of range, either upgrade them in place or pin the supported
   range via the harness's `OMNIGENT_<NAME>_VERSION_OVERRIDE`
   knob.

9. **Existing systemd unit runs a non-`omni` binary.** If the
   existing fork unit runs `python -m omnigent` from a clone at
   `/srv/omnigent/repo`, the rebuild wheel installs a `omni` shim
   to `~/.local/bin/omni` (via `uv tool install`). The new unit
   must point at the shim, not at the clone. **Mitigation:**
   Phase C.4 explicitly rewrites `ExecStart=` to
   `/usr/local/bin/omni server …`.

10. **`rebuild/upstream-0.7` is not yet built or tested
    end-to-end.** The cutover's canary (Phase D — the
    pre-cutover canary on the existing Omnigent VM, NOT on a
    disposable VM) is the next milestone. Until Phase D.1
    AND Phase D.2 both show 12/12 PASS with no SKIPPED,
    the rebuild branch is a planning artifact, not a
    production candidate. Do **not** schedule a maintenance
    window until Phase D is complete.

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
    rebuild's needs. **For the in-place fresh-DB cutover, this is
    no longer a blocker**: the legacy DB is moved aside read-only,
    and the fresh DB does not contain any of these tables. The
    legacy DB is preserved for inspection only.

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
   still work on the v0.7.0 OmniRoute endpoint?** Untested. The
   Phase D canary (on the existing Omnigent VM) must include a
   real `/routes:select` round-trip — that is acceptance check
   #3.

8. **Is the production host's `omni-control-room-host.service`
   unit file checked into the repository, or is it managed
   out-of-tree?** The fork's `install_eval_host_unit.sh` writes
   it; the rebuild's systemd unit template ships in
   `deploy/systemd/control-room/`. The live unit file is not
   checked in; the rebuild does not need to change it. **For the
   in-place fresh-DB cutover, this is moot**: there is no
   separate host unit; the single `omnigent.service` runs the
   web + host in one process (upstream v0.7's design).

9. **What is the size of the production `chat.db`?** Used only to
   size the backup file: the cutover script's SHA-256 + size
   check must handle the live file's full size. The cutover does
   not migrate the DB; backup size is the only DB size that
   matters.

10. **What is the configured `<data_dir>` on the production host?**
    Must be confirmed before the cutover: the cutover script
    reads `OMNIGENT_DATA_DIR` from the EnvironmentFile (Phase
    C.1). If the env var is unset on the existing fork unit, the
    new unit must set it explicitly to `/var/lib/omnigent` (or
    the production host's chosen path) before the cutover.

## Things to validate during the canary

Phase D runs the rebuild wheel on the **existing Omnigent VM** (NOT
on a disposable VM and NOT on another host) using the isolation
rules in `PHASED_IMPLEMENTATION_PLAN.md` §D.0a (what Phase D uses): unused temp loopback
port, temp `OMNIGENT_DATA_DIR`, fresh temp SQLite, temp artifact +
harness dirs, temp configs, disposable Git branches or a dedicated
disposable GitHub test repo. Phase D must NOT stop or restart the
production service, modify the live systemd unit, bind the production
port, touch the live `chat.db`, alter the reverse proxy, install the
rebuild wheel over the current production installation, or perform
the Phase E cutover.

Within those isolation rules, the canary must demonstrate:

- Real Pi session on the live harness binary; check that the
  watchdog survives a 10-minute quiet turn (acceptance check #4–#6).
- Real OpenCode session; check the canonical resolver picks
  `opencode-native` even with a fuzzy request (acceptance check
  #7–#9).
- Real Verity fan-out; check the per-task worktree isolation
  works for two concurrent children (acceptance check #11).
- Real Langfuse integration; check the trace hierarchy and the
  secret-redaction filter (acceptance check #10).
- Real OmniRoute combo selection; check that the
  `x-omniroute-*` headers are captured into the Langfuse trace
  (acceptance check #3 + #10).
- Real `/v1/route` round-trip with both requested and executed
  model provenance (acceptance check #3).

The canary must run twice (D.1 + D.2) — first from an empty
temporary data directory, second with the same temporary deployment
configuration to prove repeatability. Both runs must report
12/12 PASS with no SKIPPED before Phase E is authorized.

## Open PRs the audit found but did not evaluate

- `Mortified2896/omnigent` PR #65 was abandoned (per PR #66's body).
- The `fork-e2e/pr-*` branches in upstream are mirror-only and
  not in scope.
- The 23 `backup/*` and 6 `staging-v060*` branches are historical
  forks; not in scope.
