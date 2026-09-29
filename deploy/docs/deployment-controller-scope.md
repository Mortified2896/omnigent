# Deployment controller scope

Owner clarification, 2026-09-19. This document defines the scope of O1/O2
self-update restrictions. It supersedes contradictory blanket wording in
older runbooks, historical deployment records, and agent handoffs. It does not
grant deployments that the owner has not authorized or change installed tools.

## Local host comes before remote access

Before choosing any SSH/Tailscale path, run `hostname; id; pwd`. If an
independent controller is already on `rtx-omnigent`, it is already on the RTX
v2 live host and must inspect/deploy locally. Do not require
`homelab-hermes`, `proxmox-home`, or Tailscale SSH to reach the machine it is
already executing on. A Tailscale SSH policy denial is therefore not a live-host
reachability failure in that case.

The current RTX peer entrypoint explicitly requires
`hostname == rtx-omnigent` and effective UID 0. If the controller is local but
lacks the required privileged external interface, report that exact local
privilege/tooling blocker after inspecting available host-local mechanisms.
Do not replace it with a network blocker.

## Decide by the controller, not the target name

**Instance-controlled update:** the controlling task/process runs inside O1 or
O2 and depends on that Omnigent instance. O1 must not replace/restart itself
from its own task; O2 must not do so either. Use the other healthy instance for
an instance-driven update, with distinct TARGET and SUPERVISOR identities.
The peer-specific preflight and continuity rules apply to that mode.

**Externally controlled deployment:** an independent Codex Mac app/CLI, ZCode,
operator shell, or SSH session controls the deployment outside the O1/O2 task
runtime. It may directly update O1, O2, or both sequentially when authorized.
It does **not** require an O1/O2 supervisor task, peer approval, or a
TARGET/SUPERVISOR pair. It need not create a task inside the other instance.
The external controller owns validation, service operations, and recovery.
A missing/unavailable peer is not by itself a policy blocker for this mode.

Being on the same server, using the same repository, or deploying software
named Omnigent does not make an independent controller O1/O2. Conversely,
invoking Codex from an O1/O2 task does not turn that task into an independent
external controller. Establish actual execution ownership and whether the
controller survives a target service restart; do not infer it from the model
or application name.

| Actual controller | Required treatment |
| --- | --- |
| Independent Mac Codex deploys O1 over SSH | External deployment; no O2 supervisor task required. |
| Independent server Codex CLI/operator shell deploys both peers | External deployment; update one target at a time, retaining external recovery control. |
| O1 task invokes Codex to replace O1 | Still an O1 self-update; use an independent controller or the other instance. |
| O1 task deploys O2 | Instance-controlled peer update; O1 remains the healthy supervisor. |

## Safeguards common to both modes

Verify the live target and repository identities; preserve unrelated work and
active user sessions; deploy the exact tested immutable artifact; check schema
compatibility; take consistent backups; serialize target mutations; retain the
previous release and recovery evidence; verify health, embedded build identity,
and the requested UI after deployment. Use only the narrowly required host
privileges. External control is not permission to skip these safeguards.

For a two-target rollout, update one target at a time and verify it before
proceeding. Keeping the other available is continuity hygiene, not a requirement
that it run an AI task or authorize the external controller.

## Tool contracts are not universal deployment policy

`peer_deployer.rtx` and legacy peer-only entrypoints have their own required
arguments and checks. A documentation change does not add an external mode to
those programs. Do not fabricate a supervisor, add unsupported flags, revive
retired instances, or pass a pretend peer transaction merely to make a tool run.

For an authorized external deployment, use a verified external host procedure.
If the available implementation supports only peer mode, identify that concrete
tooling gap and implement/test an explicit external-controller path within the
authorized task, or report the exact missing capability. Do not reinterpret the
self-update rule as a ban on independent Codex/operator deployment. Keep common
artifact, backup, identity, and rollback protections in the external path.

## Continuation and completion

Before resuming work, fetch current GitHub refs for both Omnigent and HomeLab.
Fast-forward clean tracking checkouts, and merge/reconcile current main into an
existing feature worktree without resetting dirty or unrelated work. Read these
updated docs from the resulting checkout, not an old chat or cached branch.

When merge and deployment are explicitly authorized, the requested completion
is: validate the scoped fixes, merge to main, deploy the exact resulting artifact,
and report the live URLs, build identities, checks, and rollback release.
A draft PR alone is not completion. Authorization does not waive real data-loss
bugs or enable an unrelated AI scorer, provider change, or chat cleanup.

### External controller: rehearsed schema migrations

The external RTX controller also accepts `rehearsed-migration` artifacts.
Peer-controlled promotion remains restricted to `same-schema`. Migration
acceptance must name the exact source and destination Alembic revisions and
hash a rehearsal record for the same source build. The rehearsal must boot a
consistent disposable database copy, verify integrity, identity and existing
rows, and verify backup restoration. Promotion refuses a different live source
revision before stopping services. It backs up stopped state, lets the accepted
server perform its normal migration, and verifies the destination revision
before committing. Any startup or schema failure restores both prior state and
release. The existing post-commit same-schema rollback command deliberately
refuses a cross-schema downgrade: restore the recorded stopped-state backup
under a separately reviewed recovery operation instead; that restoration loses
writes made after the backup.

Scheduled native Codex runs can set `OMNIGENT_SCHEDULED_CODEX_ACCESS_LANE`
to an existing supported lane. This stamps the normal conversation access-lane
label before launch and does not change interactive sessions or host defaults.
The RTX Daily Brief uses `codex-direct` for vendor-native live search. A catalog
model ID by itself does not select transport; the gateway and direct catalog
may advertise the same ID.
