# Finish the existing Omnigent 0.16 upgrade — new Codex chat

Complete validation and the authorized sequential upgrade of the existing RTX O1/O2 instances.
Continue the existing integration; do not restart the upstream merge or redesign the application.
This handoff is upgrade-specific, not a new repository-wide process.

## Resume the correct work

- Repository: `Mortified2896/omnigent`; PR #210; branch `codex/upgrade-0.16`.
- RTX worktree: `/home/hermes/workspace/worktrees/omnigent/upgrade-0.16`.
- Integrated application commit: `0c9e613c02ddc6702579506124e98a6b552540a5`.
- Latest tested branch commit before this handoff: `ccba88bf03057bcb0ff3d64f0f0f0e55c9021d5f`.
- Upstream v0.16.0 ancestor: `82a74473ee4c01163c4269f4296b10a158f0e47e`.
- Fork main last observed: `973797e58be8bc5ea3dd2eeec5d1c9fe6c5941a3`.

Start with `hostname; id; pwd`, repository/remotes, branch status and worktree list. Fetch current
fork refs and inspect intervening changes. Fast-forward the existing clean task branch where
possible; preserve dirty or concurrent work. Do not reset, recreate or rebase away the integration.
If already on `rtx-omnigent`, remain local. Otherwise resolve the existing RTX connection and verify
its hostname. Independent Codex is an external controller, not an O1/O2 task.

Read applicable AGENTS.md and this handoff. Use `deploy/docs/upgrade-0.16-backend-handoff.md` for the
exact old failing test identities, and the existing upgrade/O3 audit documents only as needed.
The older preparation-only PR description and broad failure totals are superseded by these results.

## Work completed in ChatGPT, with actual executed CI

Added two temporary, branch-scoped CI workflows. Application and test source were NOT changed:
`.github/workflows/upgrade-016-sandbox.yml` and `upgrade-016-web-validation.yml`.
Neither workflow deploys or holds production/provider credentials; repository contents permission
is read-only and the existing security gate is retained. They also execute while the PR is draft.

### Sandbox comparison: PASS on both sources

Run: https://github.com/Mortified2896/omnigent/actions/runs/36886541107

Candidate `256b51cdda42df65eae341f7306549ecbbd19b90`: **34 passed, 0 failed, 0 skipped**.
Pinned upstream `82a74473ee4c01163c4269f4296b10a158f0e47e`: **34 passed, 0 failed, 0 skipped**.

These are the same 34 test identities listed in the backend handoff. The workflow ran serially,
without reruns or weakened assertions. Its JUnit verifier requires the complete, unique expected
set and rejects failures, errors, skips and xfails. Both independent user-namespace and
network-namespace bwrap probes exited 0 on both runners.

Execution used separate GitHub-hosted Ubuntu 22.04.5 VMs, ordinary runner UID 1001, Python 3.12.3
and bubblewrap 0.6.2. No AppArmor disabling, sysctl changes, privileged container, setuid changes
or capability additions were made. This demonstrates the selected contracts work in that
environment; it does NOT establish that RTX's service execution context can run them.

Artifacts were downloaded and their SHA-256 hashes and JUnit contents verified:

- Candidate artifact `11174217950`:
  `9816e4e1a0e1faf40748b49aa7951b115388c8078faee7a46739501cf35a40be`.
- Upstream artifact `11173544954`:
  `164d71787d72102c06c09d43d23011bfbc0d81dedf83468cbf6dda345d77086f`.

Artifacts include environment.json, results.xml, summary.json, nodeids.txt and pytest.txt.
Download into the existing private evidence directory before their seven-day retention expires;
verify these digests. `gh run download 36886541107 -R Mortified2896/omnigent` can retrieve them.

### Fresh frontend lint, types and build: PASS

Run: https://github.com/Mortified2896/omnigent/actions/runs/36888092758
Tested SHA: `ccba88bf03057bcb0ff3d64f0f0f0e55c9021d5f`.
Commands actually executed successfully:

```sh
pnpm install --frozen-lockfile --filter web
pnpm --dir web lint
pnpm --dir web type-check
pnpm --dir web build
```

Type-check executes the checked-out package's `tsc -b`. Lint reported zero warnings/errors.
The production build succeeded with CSS `::highlight` and large-chunk warnings. Do not change
valid highlighting syntax merely to silence the minifier; verify actual read-along rendering.

Artifact `11175371879`, SHA-256:
`e8c528c2478ae110f81cd45863f64cc6d08b73cbb1d84f13ca16cca1e3ab39c8`.
Contains source identity, composer blob/source and lint/type/build logs. This is validation
evidence, NOT an accepted deployable release artifact or a visual snapshot acceptance.

Earlier UI Snapshot run `36884989309` failed before rendering, with a TypeScript diagnostic.
The fresh exact-SHA standard frontend check above passed without an application patch. Do not
blindly fix a line from stale logs: verify checkout SHA, lockfile and command when investigating
the pinned-renderer workflow. That snapshot workflow still needs to run successfully.

The prior 33,191-pass backend total was aggregated across interrupted and focused runs, not a
single clean full-suite pass. Do not relabel that historic total as a newly passed full suite.
Several old green draft workflows skipped their actual test jobs; inspect jobs, not just badges.

## Remaining work — execute, do not repeat the merge

### 1. Resolve RTX runtime readiness, separately from code correctness

Before any policy change, reproduce minimal userns and netns probes as the normal test user and
in the actual server/host/runner launch context, using disposable isolated state. Record exact
bwrap executable/version/argv, UID, ancestor/cgroup ownership, `/proc/self/attr/current`, effective
capabilities, NoNewPrivs, seccomp, relevant read-only sysctls and contemporaneous audit denials.
Do not dump full environments, credentials or private payloads.

Previous RTX evidence implicated AppArmor `unprivileged_userns` in a UID-map denial. The
`RTM_NEWADDR: Operation not permitted` loopback failure was NOT independently root-caused.
Compare the two failures separately; inherited confinement may differ from an ordinary shell.
Compare upstream and candidate under the same RTX context where necessary.

A CI pass is not permission to deploy a runtime that cannot create its required sandbox. Conversely,
do not keep patching 34 tests individually when a shared environment precondition is failing.
Prove the production-relevant native Codex/helper path works under its intended confinement.
Do not run tests as root, disable containment, globally change AppArmor/sysctls, add broad
capabilities or use privileged Docker to manufacture a pass. If an actual host-policy change is
necessary, identify the narrow remedy and obtain owner authorization before applying it. Continue
other reversible validation while that approval is pending.

### 2. Finish actual integration and visual validation

Reuse the completed evidence when source/toolchain identity is unchanged. Re-run affected checks
after fixes; obtain a coherent final backend result in a supported test environment. Complete
outstanding real integration, backend/UI E2E, compatibility and required lint/security checks.
Do not count draft skip paths as tests or hide new regressions with xfails/skips.

Run the repository's pinned-renderer snapshot workflow on the actual candidate. The eight
previously conflicted PNGs are temporary upstream baselines, not accepted custom UI snapshots.
Review rendered differences against intended upstream layout plus retained Advisor/feedback/audio
behavior; do not blanket-update snapshots. Preserve mobile approval and selector interactions.
Do not add a second deployment framework, new blanket gates, benchmarks or unrelated cleanup.

### 3. Rehearse full recovery and final schema cutover

Existing evidence: `/home/hermes/workspace/upgrade-016-evidence/checkpoint.json`.
Disposable copies: `/srv/omnigent/preflight/upgrade-016-20261001`.
Those copies are NOT complete state-recovery backups.

Keep applied custom migration files intact. The joined revision is `d016c91f6a2d`, joining
`ll1a2b3c4d5e` and `c91f6a2d7e40`. The fresh DB and separate O1/O2 copies previously migrated;
confirm rehearsal evidence matches final source. Recheck private row/payload identity and
preference/snapshot loss risks, not counts alone. Do not expose or discard data to meet the cap.

Take target-specific, consistent DB plus complete required writable-state recovery backups using
the existing controller contract. Rehearse restoring them into disposable state, including
configuration/identity and audio/artifact references. Retain the prior release and backup hashes.
A failed cross-schema cutover requires the recorded release AND compatible stopped-state backup;
after new writes, restoring an old backup is potentially lossy and requires explicit recovery review.

### 4. Finish the authorized upgrade

Use HomeLab `docs/codex-server-workflow.md`, current deployment-controller scope and the reviewed
`peer_deployer.external_rtx` rehearsed-migration path. Discover live units/manifests/release pointers,
DB bindings and Tailscale Serve mappings; do not revive retired instances or invent supervisor tasks.

Once applicable checks, recovery rehearsal and review gates genuinely pass, merge the completed
PR #210 into fork main. Build and accept one immutable release from the exact final merged SHA;
verify embedded application/SDK/frontend identity and final schema acceptance. No release publishing.

Drain active work, deploy O2 first and verify it before deploying the SAME accepted artifact to O1.
Preserve separate state and leave the other instance available. For a failed cutover use the
reviewed recovery path; do not continue to the second target. Verify health, build/schema identity,
existing sessions and Tailscale login, native Codex start/reply/resume/model/effort, Advisor manual
and automatic/approval flows, exact response provenance and saved feedback, current OTEL traces,
Kokoro timing/playback/read-along, and one controlled scheduler path without duplicate daily briefs.
Label acceptance chats excluded from scoring and retain failures for inspection.

Preserve all agreed customizations and access/billing/privacy boundaries. Automated judges/training
stay disabled. Do not expand providers, change secrets/permissions, expose services publicly, delete
branches/data/backups or modify unrelated HomeLab services.

Last reported live O1/O2 build was `6eb0e27babadcd20be6bc661c07f0d66df325be7`, schema
`c91f6a2d7e40`; verify afresh. O1: https://rtx-omnigent.taile0361b.ts.net:1111/ ;
O2: https://rtx-omnigent.taile0361b.ts.net:2222/ . ChatGPT performed no live probe or deployment.

Finish with the actual merged/deployed SHAs, run links/results, schema, target URLs, acceptance and
rollback evidence. If blocked, name the exact failing command/context and required decision; do
not return another generic "backend validation incomplete" report or claim deferred work is done.
