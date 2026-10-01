# RTX O1/O2: upstream 0.16 reconciliation handoff

## Actual stage

This is an upgrade-preparation branch, not a deployable 0.16 build. Upstream
has NOT yet been integrated into its application source. Main, O1 and O2 were
not changed by this preparation. Do not merge/deploy this preparation alone
and call the upgrade complete. This document is specific to this upgrade,
not a new mandatory workflow for unrelated repository tasks.

Verified source references on 2026-10-01:

- Fork main after PR #208: `973797e58be8bc5ea3dd2eeec5d1c9fe6c5941a3`.
- Included OTEL cleanup / PR #209: `fe86869b40f3d43f92c33869511f0b451b72b4d2`.
- Target `omnigent-ai/omnigent` tag `v0.16.0`:
  `82a74473ee4c01163c4269f4296b10a158f0e47e`.
- Continuation branch: `codex/upgrade-0.16`; fetch its current head before work.

The branch inherits the cleanup commits without merging #209 into main.
Do not duplicate those changes or close/delete the earlier branch as cleanup.
The current tracing requirement is OTEL/OTLP against the actually configured
collector. Do not restore retired receiver-specific configuration, propagation,
or runbooks during the upstream merge. No collector replacement is requested.

## Work completed here

`deploy/scripts/upgrade_016_preflight.py` is a read-only helper, not a new
release controller. It audits supplied SQLite copies for upstream's 65,535-byte
limits and checks a candidate Alembic graph without executing migrations.

It distinguishes stored preference bytes from recompressed TEXT snapshots;
counts UTF-8 bytes including embedded NULs; recognizes already-BLOB snapshots;
flags invalid or over-budget input rather than inventing a successful scan;
reports optional columns as absent, not zero; and emits no payloads or user IDs.
The custom `model_advisor_records` payload is NOT treated as an upstream
preference and is NOT assigned the upstream preference size limit.

The graph check requires the existing feedback/advisor/audio revisions and
upstream's preference/snapshot revisions, one joined head, and byte-preserved
custom migration scripts compared with an explicit reference directory.
Missing parents, duplicate revisions, cycles and uninspected dependency-bearing
migrations fail closed. This is structural evidence only, not a schema rehearsal.

Local validation in an isolated file projection, NOT a full repository checkout:

```sh
python -m pytest tests/deploy/test_upgrade_016_preflight.py -q
# 40 passed, 3 skipped: zstandard is unavailable in this execution environment.
python -m py_compile deploy/scripts/upgrade_016_preflight.py \
  tests/deploy/test_upgrade_016_preflight.py
```

The three compression tests MUST run in the real locked environment; do not
count the skips as passes. Full pre-commit, application tests, builds, upstream
migration parity tests, live database audits and runtime acceptance were not
run here. The inherited #209 changes also still need actual regression checks.

## Continue source integration

Use the current AGENTS.md and a dedicated task worktree. Verify repository,
fetch/push destinations, branch and dirty state. Preserve unrelated work.
Fetch current main, this branch, and the exact upstream tag. Check the resolved
tag SHA against the target above; do not silently substitute upstream main or
a later version.

Keep both Git ancestries and the fork's existing commits. Integrating upstream
into this feature branch with a normal merge followed by reviewed subsystem
replacements is acceptable. Do not force-reset main, use a fake ancestry merge,
or resolve the entire tree with blanket ours/theirs choices. For generic
composer/catalog/harness code, prefer the 0.16 implementation and port only the
necessary custom integration points. Generate a complete local diff from the
actual merge base; do not infer overlap from truncated GitHub file lists.

Required behavior to retain:

- Model Advisor, current automatic Send behavior and per-model approval gates;
  ordinary human model/effort selector plus separate advisor selector; saved
  provider/model masks retaining selected efforts; independent advisor choice;
  qualified Direct/OmniRoute/GLM transport policy; allowed-pool snapshots;
  durable rounds and at-most-once decision/dispatch. Preserve current settings
  and historical v1/v2 rounds, not obsolete design-document restrictions.
- Human thumbs/comments, outcomes/tags and exclusions. Keep human review data
  out of outbound advisor/evaluator context. Do not enable automated judges,
  training, new benchmark pipelines or new provider/account fallbacks.
- Response-specific requested versus observed model/effort/route and exact
  advisor-round binding. Retain the implementation until upstream proves
  equivalent response-level provenance. Session labels, model changes and
  aggregated session usage alone are NOT that proof; unknown stays unknown.
- Generated-response audio, existing recordings/artifacts, Kokoro scheduled
  narration, timing sidecars, MP3 fallback, seeking and word highlighting.
  Use upstream scheduling and add only the search/audio integrations; do not
  introduce a second scheduler or duplicate the existing daily-brief trigger.
- Trusted Tailscale identity mapping to existing accounts, password/Bearer
  recovery, distinct O1/O2 cookies/secrets/host identities and database binding.
  Preserve loopback-only backends and private Serve exposure; no Funnel.
- Current OTEL end-to-end propagation, service/session/response identity,
  privacy/content-capture behavior and explicit signal configuration. Verify
  trace topology rather than blindly preserving old parent-rewriting code.
- Current immutable artifact, backup, serialized promotion, restart-failure
  recovery and external-controller safeguards. HomeLab owns installed wiring;
  Omnigent owns application and portable deployment code.

Retirement candidates: disabled O3 routing experiments, benchmark capture,
old tool-free experiments and legacy deployment shims. A disabled flag is NOT
proof that code has no callers. Trace imports, shared helpers, API consumers,
configuration and data readers first. Extract any needed helper and add parity
coverage before removal. Preserve uncertain code disabled; do not delete stored
history, evaluations, recordings, old releases or backups as part of this port.
Reapply #208's CI gating and runtime regression fixes only where needed on 0.16;
keep useful tests and the lean fork AGENTS/contribution workflow.

## Schema and data acceptance

Keep the deployed custom scripts and their revision/down_revision values intact:

```text
ge1b2c3d4e5f -> f8a9b0c1d2e3 -> b4d8e2f6a9c1 -> c91f6a2d7e40
```

Upstream independently continues from the common history. Inspect the actual
complete 0.16 graph and add a NEW merge revision joining its actual head with
the custom head. Do not guess the latest head by filename or reparent an
already-deployed migration. Preserve fresh-install and existing-fork paths.

Upstream `kk1a2b3c4d5e_preferences_value_blob.py` discards oversized stored
preferences; `ll1a2b3c4d5e_compress_inference_snapshot.py` clears snapshots that
remain oversized after compression. Read the pinned sources, including their
stopped-reader/writer and transaction requirements. A generic outer transaction
is not automatically a safe wrapper for their batched conversion.

Create independent, consistent private backup copies for the actual O1 and O2
DB/state bindings; include conversation databases and artifact references if
storage is split. Export original custom migration files from the verified fork
reference into a temporary reference directory. Then run, with real paths:

```sh
uv run --locked --no-sync python deploy/scripts/upgrade_016_preflight.py \
  --database O1=/private/backup/o1.sqlite \
  --database O2=/private/backup/o2.sqlite \
  --migrations omnigent/db/migrations/versions \
  --reference-migrations /private/reference/versions
```

This helper is SQLite-only. For another actual backend, use backend-native
read-only inspection; do not claim this check covered it. A nonzero exit or
oversized/unreadable data blocks cutover until a reviewed data-preserving
handling is established. A backup alone does not justify silently discarding
settings. A zero exit still says `deployment_accepted: false` by design.

Run the exact candidate's real Alembic upgrade and application boot on both
copies, plus a fresh DB. Verify custom and upstream schema, ownership, row IDs,
private payload hashes, schedules, session/response bindings and retained audio;
prove restoration to the original backup/release. Counts alone are insufficient.
Do not publish backups, payloads or credentials to GitHub. Check current live
schema/SHA again immediately before any service mutation.

## Validation and live finish

Install/reconcile the supported locked Python and pnpm environments, matching
all three package versions and generated API/SDK code. Run the new tests with
zstandard installed, the inherited OTEL suites and host-environment tests, then
the impacted advisor/feedback/attribution/auth/audio/scheduler/migration tests.
Run real lint/type checks and the release build. Broaden to the normal Python,
web, backend/UI E2E, snapshot and compatibility jobs for this cross-cutting
upgrade. Do not count draft/empty-matrix skips as executed checks or refresh
snapshots without checking the changed behavior. Record exact tested SHAs.

Use an independent Codex/controller outside O1/O2. Read current HomeLab
`docs/codex-server-workflow.md`, Omnigent
`deploy/docs/deployment-controller-scope.md` and `deploy/docs/rtx-peer-v2.md`.
Run `hostname; id; pwd` first. If already on `rtx-omnigent`, stay local; otherwise
use a verified existing route. Discover live manifests, units, release pointers,
DB bindings, effective OTEL configuration and Serve mappings; do not revive old
instances or invent a peer supervisor task. For changed schemas use the reviewed
external `rehearsed-migration` path, not the same-schema-only peer path.

After successful integration/review/validation, complete the requested fork
merge and upgrade of the existing O1/O2 instances. Build and accept one immutable
artifact for the exact final source SHA, draining active work before its target
restart. Prefer O2 as the first canary while O1 remains available, provided fresh
live-state checks permit that order. Verify it before deploying the same
artifact to O1, preserving each target's distinct state/configuration. Do not
restart both together or change unrelated services, credentials or permissions.

Exercise a marked, excluded acceptance session: native Codex start/reply/resume,
advisor/manual selection and approval, model/effort provenance, feedback reload,
Tailscale account access, reconnect, and an actual completed OTEL trace. Exercise
Kokoro audio with timings and read-along, and the scheduler fire path without
duplicating the real brief. Keep failures and evidence for inspection.

Finish with merged SHA, O1/O2 live URLs and observed builds, real check results,
schema revisions, rollback release/backup locations and any remaining limits.
Do not publish a release or delete branches, data, test chats or recovery assets
unless separately authorized. When a real blocker remains, stop before unsafe
mutation and report exactly what remains; do not label a prepared branch an
upgraded deployment.
