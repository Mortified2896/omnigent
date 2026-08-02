# Final audit report

## Branch and tag refs

| Ref | SHA | Resolves to |
| --- | --- | --- |
| `refs/heads/rebuild/upstream-0.7` (NEW) | `ea4b9fb9b10388aa646b67d0302e781ae9ca9629` | `35519fb04743f66b30cac8a40695d5d72fa163ea` (v0.7.0) + 1 commit (`ea4b9fb9 docs(rebuild): Phase 1-2 audit deliverables on rebuild/upstream-0.7`) |
| `refs/heads/legacy/custom-pre-0.7` (NEW) | `a4f20cab1fe69be63cb9794218a94e521af0c6f0` | the production fork's checked-out commit at audit start |
| `tag: legacy-custom-pre-0.7-20260802` (NEW) | `969a160a38a7d9e070608ec8ab8d6e35d00506a4` | same |
| `refs/heads/legacy/custom-pre-0.7-remote-main` (NEW) | `c695007ce0b19641f3b5ed474f3ec884ec2f555c` | production fork remote `main` HEAD at audit time |
| `tag: legacy-custom-pre-0.7-remote-main-20260802` (NEW) | `4c13baed2bc3dd1c19a83ee0a33e51530d8cfb4e` | same |
| `refs/heads/legacy/custom-pre-0.7-staged-20260802` (NEW) | `1b8f15039c3acdbaf329b807bc386a5adf70e2a1` | the dirty index of the production checkout, frozen into a commit on top of `a4f20cab` |
| `tag: legacy-custom-pre-0.7-staged-20260802` (NEW) | `d866c753dd847212f89bef90b97962053af6230e` | same |
| `refs/remotes/fork/main` (UNTOUCHED) | `c695007ce0b19641f3b5ed474f3ec884ec2f555c` | unchanged |

Upstream v0.7.0 tag SHA: `35519fb04743f66b30cac8a40695d5d72fa163ea` (re-verified
via `git show-ref --verify refs/tags/v0.7.0` and `git ls-remote --tags origin v0.7.0`).

## Files changed on `rebuild/upstream-0.7`

`docs/rebuild/` (12 new files, 2 104 insertions):

| File | Lines | Purpose |
| --- | --- | --- |
| `README.md` | 66 | landing page |
| `REPOSITORY_STATE_REPORT.md` | 144 | Phase 1 ground truth |
| `MIGRATION_MATRIX.md` | 169 | Phase 3 classification of 119 custom commits |
| `ARCHITECTURE_PROPOSAL.md` | 174 | Phase 4 minimal architecture |
| `OMNIROUTE_PLAN.md` | 127 | Phase 5 OmniRoute integration |
| `LANGFUSE_PLAN.md` | 159 | Phase 7 Langfuse via OpenTelemetry |
| `HARNESS_GIT_PLAN.md` | 119 | Phase 6 Pi/OpenCode Git flow |
| `AUTO_UPDATE_PLAN.md` | 78 | Phase 9 update recommendation |
| `ACCEPTANCE_TESTS.md` | 399 | Phase 8 15-test specification |
| `PHASED_IMPLEMENTATION_PLAN.md` | 147 | 7-phase gated plan |
| `RISKS_AND_UNRESOLVED.md` | 203 | 10 production risks + 5 non-production + 10 unresolved |
| `EVIDENCE_INDEX.md` | 319 | file/line citations for every matrix claim |

No file outside `docs/rebuild/` was added or modified. No upstream
file was patched. No core code was changed. No tests were run; no
LLM calls were made.

## Tests run

None. The audit's stop point is "investigation, preservation, clean
branch creation, migration matrix, architecture, and test plan".
Per the spec, "do not port large groups of custom commits or modify
production until the audit clearly identifies what is required." The
15 acceptance tests in `ACCEPTANCE_TESTS.md` are *specifications*
for the canary, not tests run during this audit.

The following validation commands **were** run, all read-only:

- `git rev-parse HEAD`, `git rev-parse v0.7.0^{commit}`, `git rev-parse v0.6.0^{commit}`
- `git ls-remote --tags origin v0.7.0 v0.7.0rc1 v0.6.0`
- `git merge-base --is-ancestor` (between `a4f20cab`, `35519fb0`, `375f5404`)
- `git diff --stat v0.7.0 refs/heads/legacy/custom-pre-0.7`
- `git log --no-merges --cherry-pick --right-only v0.7.0...refs/heads/legacy/custom-pre-0.7`
- `git log --first-parent 255a5f8f..refs/heads/legacy/custom-pre-0.7`
- `git show --stat --find-renames` for every key topic commit
- `git grep` against `v0.7.0` for each cross-reference in the
  migration matrix (live counters: 11 388 hits on `v0.7.0` for
  the keyword scan, 402 hits on `refs/heads/legacy/custom-pre-0.7`)
- `gh pr view` / `gh issue view` / `gh issue list --search` against
  `Mortified2896/omnigent` (24 PRs/issues fetched) and
  `omnigent-ai/omnigent` (12 PRs/issues searched)
- `git fetch --unshallow origin` (preserved at
  `/tmp/omnigent-audit-20260802/fetch-origin-unshallow.log`,
  `fetch_exit=0`, sha256 `566c5df47d63ce7269b3d9b69d75f9b774ca775fd8af61c6f988b20dd481b6ea`)
- `git show-ref --verify` against every preservation ref (all present)
- `git branch --points-at HEAD` (clean rebuild branch on top of v0.7.0)
- `git push --set-upstream fork rebuild/upstream-0.7` (preserved at
  `/tmp/omnigent-audit-20260802/push-rebuild.log`,
  `push_exit=0`, sha256 `5c5db60d8007809f69eaba7eefb0b58eea441ef837e18efaa5c6eab5d93db7b6`,
  fork remote now lists `refs/heads/rebuild/upstream-0.7 = ea4b9fb9…`)

## What is proven

1. **The fork and upstream v0.7.0 share a 2026-07-09 common ancestor
   at `255a5f8f10bfe6eec224d6f3e1cd0c262d6d0a85`, but neither release
   tag is an ancestor of the other** — the rebuild must start at the
   v0.7.0 tag, not at fork/main.
2. **The upstream v0.7.0 tree already has every capability the
   rebuild needs at the architecture level** (OpenTelemetry +
   `OMNIGENT_TELEMETRY_ENABLED` master opt-in + session-id
   span attribute + payload redaction; `OpenAICompatibleAdapter`
   for any OpenAI-compatible gateway including OmniRoute; the
   `routing-mvp` `ExternalRoutingClient`; the
   `OPENCODE_NATIVE_CODING_AGENT` declaration;
   `prune_invalid_sub_agents=True` at every spec-load site; the
   per-session worktree API in `routes/hosts.py` and
   `host/git_worktree.py`; the `process_manager.py:OMNIGENT_HARNESS_TMP_PARENT`
   env hook; the `omni upgrade` flow with `--drain` and
   `--force`; the `release.yml`/`finalize-release.yml` rc→final
   pipeline). Verified by file/line citations in
   `EVIDENCE_INDEX.md`.
3. **The custom work is dominated by deployment / updater / harness
   safety**, not by core runtime changes. Of the 119 non-merge
   custom commits, only the liveness-aware watchdog and the identity-
   scoped `HostRegistry.deregister` are appropriate for upstream
   (and only because they are small, isolated, well-tested patches
   of real bugs).
4. **The preservation refs are in place and the production checkout's
   working tree is unchanged**. `git status --short --branch` on the
   production checkout still shows the 14 modified files and the
   untracked `worktrees/` from audit start.
5. **The rebuild branch tip is parented exactly on the upstream v0.7.0
   tag** (`git rev-parse ea4b9fb9^ == 35519fb0`).

## What is unproven / pending canary

1. **None of the 15 acceptance tests have been run** — the canary
   (Phase E) is the next milestone. The tests are designed to run
   against the rebuild branch's `deploy/control-room/` layer plus a
   fresh `omnigent==0.7.0` wheel.
2. **No end-to-end OmniRoute round-trip has been verified live**.
   Phase E must include a real `/routes:select` exchange with the
   deployed OmniRoute instance.
3. **No real Langfuse integration has been verified live**. Phase E
   must include a real OTLP export to a Langfuse sandbox and an
   inspection of the resulting trace hierarchy.
4. **The migration rehearsal on a disposable copy of the production
   database has not run**. Phase E must run `alembic upgrade head`
   on a copy of the live `chat.db` and confirm the head is
   `zf1a2b3c4d5e` (or, with the fork-only repair migration applied,
   `zg1a2b3c4d5e`) with no script-level changes.
5. **The `agent_selector.py` and `opencode_provenance_proxy.py` work
   has not been measured against v0.7.0's behavior** — the matrix
   flags this as "already solved upstream" but the canary should
   run a positive test (fuzzy OpenCode request, observe
   `opencode-native` resolution) and a negative test (Verity's
   text match, observe rejection) before declaring it unnecessary.
6. **The two narrow upstream-quality PRs (liveness watchdog,
   identity-scoped `HostRegistry.deregister`) have not been written**.
   They are Phase C.1 and Phase C.2 of the implementation plan and
   are scoped for the next task.

## Recommended next step

Move to **Phase C (the two narrow upstream-quality PRs)** so the
rebuild branch has its first actionable core change. The work is
bounded:

- A new `omnigent/runtime/harnesses/watchdog.py` (~700 LOC + 27
  unit tests) plus a 4-budget update to
  `omnigent/runtime/harnesses/_scaffold.py:_guarded_run_turn`.
- An `deregister(connection: HostConnection | str) -> bool` overload
  in `omnigent/server/host_registry.py` (~50 LOC + 4 tests) plus
  a call-site update in `omnigent/server/routes/host_tunnel.py`.

Both are small, well-tested, and unambiguously upstream-quality.
Neither requires the production database or any service. After
Phase C is complete, Phase D (the Control-Room layer) can begin
in a sibling worktree or a sibling repository.
