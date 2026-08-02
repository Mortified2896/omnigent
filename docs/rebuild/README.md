# `rebuild/upstream-0.7` — landing

This directory is the **investigation-first** deliverable for the
Omnigent-rebuild task. Nothing here runs the production installation;
nothing here is deployed; nothing here changes `fork/main`, the live
release, the production database, or the production systemd units.

## Goal

The new upstream-based Omnigent 0.7 installation **becomes** the
production instance on the existing website and the existing
production port. There is **no second permanent install**, no second
hostname, no second port, no second public endpoint. The phone URL
keeps working. The only acceptable downtime is the brief restart
required to replace the running service.

## Database strategy

The live production `chat.db` is **disposable**. The existing
fork-lineage schema is **not** migrated into upstream v0.7. During
the cutover, the existing `chat.db` is **moved aside** to a
timestamped, read-only backup, and a brand-new upstream 0.7
database is initialized at the same configured database URI.
**No sessions, agents, scheduled tasks, conversation records, or
database configuration are imported from the legacy database.**
Only the items listed in
`PHASED_IMPLEMENTATION_PLAN.md` §C.3 are recreated from files /
environment.

## Contents

| File | Purpose |
| --- | --- |
| `REPOSITORY_STATE_REPORT.md` | Phase 1: identity, remotes, ancestry, branch topology, preservation refs, safety checklist. |
| `MIGRATION_MATRIX.md` | Phase 3: 119 non-merge custom commits classified into 7 categories (already solved upstream, port, deployment, database, obsolete, unclear, upstream-contribution). |
| `ARCHITECTURE_PROPOSAL.md` | Phase 4: minimal custom architecture (Control-Room layer, narrow core patches, no core churn). |
| `OMNIROUTE_PLAN.md` | Phase 5: OmniRoute integration via stock OpenAI-compatible provider, no `omnigent/` change required. |
| `LANGFUSE_PLAN.md` | Phase 7: Langfuse via stock OpenTelemetry exporter + `OMNIGENT_TELEMETRY_ENABLED` master opt-in. |
| `HARNESS_GIT_PLAN.md` | Phase 6: Pi/OpenCode use stock `git` and stock worktree API, no custom commit protocol. |
| `AUTO_UPDATE_PLAN.md` | Phase 9: stock `omni upgrade` for source; custom control-room scripts for production promotion. |
| `ACCEPTANCE_TESTS.md` | Phase 8 (contract): 15 fresh-DB acceptance tests that are exercised as **a subset of the 12 Phase D checks** on the existing Omnigent VM; the suite assumes a fresh 0.7 database with no legacy state imported. |
| `PHASED_IMPLEMENTATION_PLAN.md` | In-place replacement on a **fresh 0.7 database**: Phases A–G. **Phase D is a pre-cutover canary on the existing Omnigent VM** (NOT on a disposable VM and NOT on another host); §D.0a + §D.0b enumerate the strict isolation rules. **Phase E remains the only in-place production operation.** |
| `ACCEPTANCE_TESTS.md` | 15 fresh-DB acceptance tests + the "removed tests" rationale (vs. the prior 15-test spec). |
| `AUDIT_REPORT.md` | The original audit's final report (SHAs, branches, files, what is proven, what is unproven, recommended next step). |
| `EVIDENCE_INDEX.md` | File/line citations for every claim in the migration matrix. |
| `RISKS_AND_UNRESOLVED.md` | 10 production-bound risks, 5 non-production risks, 10 unresolved questions, and the canary validation list. |

## How to read this

1. `PHASED_IMPLEMENTATION_PLAN.md` — the current ordering of work,
   including the production cutover (Phase E) and rollback (Phase G).
2. `REPOSITORY_STATE_REPORT.md` — establishes the ground truth.
3. `MIGRATION_MATRIX.md` — decides what is in scope.
4. `ARCHITECTURE_PROPOSAL.md` — decides how the in-scope items are
   laid out (Control-Room layer + two narrow core patches).
5. The four `*_PLAN.md` files — decide the contract for each concern.
6. `ACCEPTANCE_TESTS.md` — proves the contract works.
7. `RISKS_AND_UNRESOLVED.md` — what can still bite us.
8. `EVIDENCE_INDEX.md` / `AUDIT_REPORT.md` — verification anchors.

## Branch and tag refs (Phase 1 preservation)

| Ref | Resolves to | Created from |
| --- | --- | --- |
| `refs/heads/legacy/custom-pre-0.7` | `a4f20cab1fe69be63cb9794218a94e521af0c6f0` | (production fork's checked-out commit) |
| `tag: legacy-custom-pre-0.7-20260802` | `a4f20cab1fe69be63cb9794218a94e521af0c6f0` | same |
| `refs/heads/legacy/custom-pre-0.7-remote-main` | `c695007ce0b19641f3b5ed474f3ec884ec2f555c` | (production fork remote `main` at audit time) |
| `tag: legacy-custom-pre-0.7-remote-main-20260802` | `c695007ce0b19641f3b5ed474f3ec884ec2f555c` | same |
| `refs/heads/legacy/custom-pre-0.7-staged-20260802` | `1b8f15039c3acdbaf329b807bc386a5adf70e2a1` | (dirty working-tree index on top of `a4f20cab`) |
| `tag: legacy-custom-pre-0.7-staged-20260802` | `1b8f15039c3acdbaf329b807bc386a5adf70e2a1` | same |
| `refs/heads/rebuild/upstream-0.7` | `35519fb04743f66b30cac8a40695d5d72fa163ea` | (upstream `v0.7.0` tag) |

None of these references may be moved. The preservation refs are
intentionally outside the rebuild branch so a stray force-push of
`rebuild/upstream-0.7` cannot destroy them.

## Production safety

Every action in this audit was a **read** against the local repository
or a single `git fetch --unshallow origin` against the upstream remote.
The production installation's:

- repository
- database
- releases
- systemd units
- deployment scripts
- `deployed-sha` markers
- branches, tags, and worktrees

are all unchanged. See `REPOSITORY_STATE_REPORT.md` §7 for the
explicit checklist.
