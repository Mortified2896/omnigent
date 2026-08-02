# Repository state report — pre-rebuild audit

Date (UTC): 2026-08-02
Auditor: Pi session (rebuild investigation)
Scope: establish ground truth before any new branch work; no production
service was modified.

## 1. Identity & remotes

| Item | Value |
| --- | --- |
| Working clone | `/home/hermes/workspace/repos/omnigent-eval` |
| Local HEAD before audit (checked-out) | `a4f20cab1fe69be63cb9794218a94e521af0c6f0` (`fork/main`, dirty index + untracked `worktrees/`) |
| Local branch `main` HEAD | `8ec97d438733a2a6ed3ce2f0eb9939c6c5976e64` |
| `origin` remote | `https://github.com/omnigent-ai/omnigent.git` (upstream) |
| `fork` remote | `https://github.com/Mortified2896/omnigent.git` (production fork) |
| `fork-ssh` remote | `git@github.com:Mortified2896/omnigent.git` (production fork, SSH) |
| Upstream HEAD before audit | `297425b08bca0aac64de126dc634a094714904e8` (`origin/main`) |
| Upstream release tag `v0.7.0` | `35519fb04743f66b30cac8a40695d5d72fa163ea` (author: `omnigent-ci[bot]`, 2026-07-27) |
| Upstream release tag `v0.7.0rc1` | `621894eb70dd410f03bbd280f0a94d12df7501f7` |
| Upstream release tag `v0.6.0` | `375f540421baf3ad46fae0805b78063682f281de` (2026-07-21) |

## 2. Shallow history

The clone was shallow with boundary `3b42ccbebbcca8cc7215ea3e4ac0da63c0be2654`
(the `#38` "drop deliberately-broken migration" commit). The audit ran
`git fetch --unshallow origin` against the upstream remote; the resulting
fetch log is preserved at
`/tmp/omnigent-audit-20260802/fetch-origin-unshallow.log`
(`fetch_exit=0`, SHA-256 `566c5df47d63ce7269b3d9b69d75f9b774ca775fd8af61c6f988b20dd481b6ea`).

Production-side fetches against the `fork` remote were deliberately **not**
performed during the audit; the working tree continues to show only the
shallow `fork/main` snapshot it had at start.

## 3. Ancestry vs upstream v0.7.0

```
v0.7.0 (= 35519fb0)        ✓ reachable from upstream main
v0.6.0 (= 375f5404)        ✓ reachable from upstream main
v0.7.0 is ancestor of upstream/main?   yes (no forks ahead of v0.7.0 in upstream)
fork/main@a4f20cab         contains v0.6.0 (verified)
                          v0.7.0 is NOT an ancestor of fork/main@a4f20cab
                          v0.6.0 is NOT an ancestor of upstream v0.7.0
                          (→ upstream and fork diverged at a 2026-07-09 commit,
                          before v0.6.0 was tagged)
fork/main@a4f20cab  vs  v0.6.0
  base = 255a5f8f10bfe6eec224d6f3e1cd0c262d6d0a85 (2026-07-09)
  custom-only:    135
  upstream-only:  352
fork/main@a4f20cab  vs  v0.7.0
  base = 255a5f8f10bfe6eec224d6f3e1cd0c262d6d0a85 (same point — v0.7.0 is
         the next upstream commit after the divergence, not a descendant of it)
  custom-only:    135
  upstream-only:  575  (including 352 carried by v0.6.0)
```

Conclusion: the production fork **does not** sit on a clean 0.6 → 0.7
linear history. It branched away from upstream pre-v0.6.0, added 135
first-parent commits of custom work, and has been force-merged against
upstream `main` (not against release tags). A `rebuild/upstream-0.7` branch
must therefore start at `35519fb0` (v0.7.0 tag) and not at any
`fork/main`/`origin/main` tip.

## 4. Branch topology (local + remote, relevant subset)

| Branch | Type | Notes |
| --- | --- | --- |
| `main` (local) | production | Currently `8ec97d43`; one commit ahead of the checked-out `fork/main`. |
| `fork/main` (remote) | production remote HEAD | `c695007c` (newer than local `fork/main@a4f20cab`); merges `fix/updater-controller-rollback-verification` (PR #70). |
| `rebuild/upstream-0.7` | **new** | Created at `35519fb0` per Phase 2. Worktree at `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7`. |
| `legacy/custom-pre-0.7` | **new** | Created at `a4f20cab` (the audited working-tree commit) per Phase 1. |
| `legacy/custom-pre-0.7-remote-main` | **new** | Created at `c695007c` (remote fork/main HEAD at audit time) per Phase 1. |
| `legacy/custom-pre-0.7-staged-20260802` | **new** | Created at `1b8f15039c3acdbaf329b807bc386a5adf70e2a1`; preserves the dirty index of the checked-out fork (8 deployment/updater/host files, plus untracked `worktrees/`). |
| `backup/*` | historical forks | 10 named backup branches captured earlier. |
| `feat/issue-17-oversight-autopilot-contract` | feature PR | part of the unmerged autopilot / routing cluster. |
| `feat/issue-18-durable-run-persistence` | feature PR | `zd1 → ze1` Alembic lineage; issue-run atomic-leasing schema. |
| `feat/issue-38-external-self-update-controller` | feature PR | the entire custom updater stack (PRs #44, #48, #51, #52). |
| `feat/model-routing-agent` | feature PR | the `agent_selector` resolver stack (PRs #56/#57). |
| `feat/per-model-call-provenance` | feature PR | `opencode_provenance_proxy.py` + `StructuredRuntimeProvenance`. |
| `feat/route-approval-mvp` | feature PR | OmniRoute combo selection + route-approval gate. |
| `feature/omniroute-combo-selection(-clean)` | feature PR | combo selection (rebase/clean variants). |
| `fix/updater-controller-rollback-verification` | feature PR | cmdline-based host-pin rollback verification (PR #70). |
| `fix/host-registry-reconnect` | server fix | canonical host-id reconciliation (PR #64). |
| `fix/host-pinned-immutable-runtime` | deployment fix | immutable runtime pin (PR #63). |
| `fix/liveness-aware-watchdog` | runtime fix | liveness-aware watchdog (PR #30/#36). |
| `fix/release-*` | deployment fix | release candidate integrity, manifest canonical path, etc. |
| `temp-hold-staging-v060` | snapshot | the v0.6 staging marker, kept until the rebuild branch is verified. |
| `fix/disable-premature-idle-watchdog` | runtime fix | watchdog default flip (PR #43). |

## 5. Working-tree & dirty-index snapshot

```
$ git status --short --branch   (primary checkout at audit start)
## fork/main
M  deploy/systemd/omnigent-updater.service.template
M  omnigent/server/host_registry.py
M  omnigent/server/routes/host_tunnel.py
M  omnigent/updater/controller.py
M  omnigent/updater/layout.py
M  omnigent/updater/maintenance.py
M  scripts/promote_release.sh
M  scripts/rollback_release.sh
M  scripts/write-dropin.sh
M  tests/server/test_host_registry.py
M  tests/updater/test_controller.py
M  tests/updater/test_maintenance.py
M  tests/updater_acceptance/test_staging_broken_candidate.py
M  tests/updater_acceptance/test_staging_success.py
?? worktrees/
```

The 14 modified files are exactly the deployment / updater / host-reconnect
cluster that this audit exists to triage. A clean index snapshot is at
`/tmp/omnigent-audit-20260802/legacy-index-a4f20cab1fe69be63cb9794218a94e521af0c6f0.patch`
(803 lines, sha256 `7d33e00be65d190b4ab5f234b3c45922a4011591fee939fce9d3a64eedbf19dc`).
The untracked `worktrees/` directory (≈3.0 GB of build artifacts) is excluded
from the preservation tag on purpose.

## 6. Preservation references created (Phase 1, do not move)

| Ref | Resolves to | Notes |
| --- | --- | --- |
| `refs/heads/legacy/custom-pre-0.7` | `a4f20cab1fe69be63cb9794218a94e521af0c6f0` | exact checked-out commit |
| `tag: legacy-custom-pre-0.7-20260802` | `a4f20cab1fe69be63cb9794218a94e521af0c6f0` | named annotated tag |
| `refs/heads/legacy/custom-pre-0.7-staged-20260802` | `1b8f15039c3acdbaf329b807bc386a5adf70e2a1` | commit on top of `a4f20cab` that bakes the dirty index tree `f3ad6470a86077cbbf05a8d4bc44b9341b05f265` into history |
| `tag: legacy-custom-pre-0.7-staged-20260802` | `1b8f15039c3acdbaf329b807bc386a5adf70e2a1` | named annotated tag for the staged index |
| `refs/heads/legacy/custom-pre-0.7-remote-main` | `c695007ce0b19641f3b5ed474f3ec884ec2f555c` | remote fork/main HEAD at audit time |
| `tag: legacy-custom-pre-0.7-remote-main-20260802` | `c695007ce0b19641f3b5ed474f3ec884ec2f555c` | named annotated tag |

## 7. Safety checklist (production)

- [x] The currently-running production installation was **not** modified.
- [x] The `fork/main` branch ref was **not** moved; only new refs were
      created and recorded in §6.
- [x] The fork's databases, releases, systemd units, and the
      `/var/lib/omnigent/shared/deployed-sha` file were **not** touched.
- [x] No PRs were opened against `Mortified2896/omnigent` or
      `omnigent-ai/omnigent`.
- [x] No new commits were pushed to any remote during the audit.
- [x] The dirty index and untracked `worktrees/` are preserved via
      `legacy/custom-pre-0.7-staged-20260802` and the raw
      `/tmp/omnigent-audit-20260802/legacy-index-*.patch` artifact; nothing
      has been discarded.
