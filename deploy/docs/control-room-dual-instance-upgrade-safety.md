# Control Room dual-instance upgrade safety

This document is the authoritative deployment-safety rule for the Control Room
O1/O2 installation. It exists to prevent an Omnigent instance from trying to
replace the runtime that is currently executing and supervising its own task.

## Non-negotiable invariant

**O1 upgrades O2. O2 upgrades O1. An instance never performs its own normal
upgrade.**

In operational terms:

- Omnigent 1 (O1) is the maintenance/secondary instance and supervises upgrades
  of Omnigent 2.
- Omnigent 2 (O2) is the production/primary instance and, after its candidate has
  passed acceptance and soak, supervises promotion of that exact accepted
  artifact to O1.
- The supervising instance must remain running and reachable for the entire
  target stop/install/migrate/start/acceptance/rollback operation.
- If a task would require O1 to stop/restart/replace O1, or O2 to
  stop/restart/replace O2, **STOP** and move the operation to the healthy peer.
- A detached self-update executor is an emergency recovery mechanism only. It is
  not an acceptable normal deployment path.

This rule overrides less-specific wording elsewhere such as "promote to O1 in a
maintenance window". Promotion to O1 means **O2 supervises O1**. Deployment to
O2 means **O1 supervises O2**.

## Normal rollout

```text
authoritative GitHub main @ source SHA S
              |
              v
      immutable artifact R
              |
              |  O1 supervises
              v
      O2 production @ R
              |
      acceptance + soak
              |
              |  O2 supervises
              v
      O1 maintenance @ R
```

Temporary version skew during rollout is intentional:

```text
before:       O1 = old     O2 = old
phase A:      O1 = old     O2 = new   (O1 is supervisor)
phase B:      O1 = new     O2 = new   (O2 is supervisor)
```

Both instances ultimately track the same authoritative `main` lineage and use
immutable exact-SHA release artifacts. Their operational roles and rollout
cadence differ; they are not separate long-lived product branches.

## Mandatory preflight question

Before any Control Room deployment or promotion, the operator/agent must answer:

1. Which instance is the **target** being changed?
2. Which different instance is the **supervisor** that will remain alive?

The pair must be one of exactly these:

- target O2 -> supervisor O1
- target O1 -> supervisor O2

If target == supervisor, **do not proceed**.

## Privileged host actions

The supervising Omnigent instance does not need unrestricted root privileges.
Privileged filesystem, systemd, database-restore, and release-switch actions
should be performed by a narrowly scoped host-level deployment/recovery
mechanism which the healthy peer invokes and observes.

The control relationship is therefore:

```text
healthy peer Omnigent
        |
        | request + supervise
        v
privileged host deployer
        |
        v
target Omnigent instance
```

Do not weaken `NoNewPrivs`, sudoers, SSH, mounts, or service isolation merely to
make a peer able to perform an upgrade.

## Exact-artifact rule

After O2 acceptance/soak, O1 receives the exact immutable artifact that was
accepted on O2. Do not rebuild from a branch tip during promotion.

The source tip may advance for deployment-only tooling fixes, but the runtime
identity being promoted must remain explicit and truthful. Do not claim a
runtime is at a source SHA it was never built from.

## Transaction and rollback safety

Deployment tooling must be fail-closed and transaction-aware.

- Preflight runs before the first destructive mutation.
- A failure before the first committed deployment mutation must be incapable of
  deleting, renaming, replacing, restoring, or otherwise modifying the active
  runtime.
- Rollback cleanup may delete only resources carrying an explicit
  transaction/release identity created by that same promotion attempt.
- Never infer that an existing active runtime directory is a newly-created
  release merely because a later expected staging/legacy path does not exist.
- Application rollback and database rollback are a pair. Never restore an old
  application against a migrated incompatible database, or vice versa.
- Preserve the old runtime and a verified DB backup until post-promotion
  acceptance completes.
- A failed attempt must not automatically retry in a loop.

## Incident lesson: 2026-08-08

During the Control Room 0.9 rollout, O1 was mistakenly instructed to supervise
its own promotion. A detached executor was introduced to survive the expected
self-restart. The actual promotion preflight refused before installation, but
fallback rollback logic then misidentified the original `/opt/omnigent/venv` as
a newly-created release path and deleted it. O1's database remained intact, but
O1 became unreachable until host-level recovery.

This incident is the reason the peer-supervision invariant above is a hard rule,
not a preference.

## Agent stop conditions

An agent must stop and report instead of proceeding when any of these are true:

- target instance and supervising instance are the same;
- the healthy peer would need to restart itself to complete the operation;
- exact target runtime/artifact identity cannot be proven;
- the peer cannot obtain a legitimate pre-existing host deployment channel;
- rollback would require guessing which files belong to the current transaction;
- DB/runtime rollback pairing cannot be proven;
- another promotion/rollback executor may still be active;
- the operation would require weakening host security to obtain privileges.

## Required evidence for every rollout leg

Record at minimum:

- target instance and supervisor instance;
- target pre-state: runtime SHA/version, services, restart counters, DB schema;
- supervisor guard state proving it remained untouched;
- exact immutable artifact SHA/hash;
- preflight result;
- DB backup and rollback identity;
- exact service stop/start sequence;
- target post-state and health;
- supervisor post-state proving zero unintended drift;
- final PASS/FAIL/ROLLED-BACK verdict.

## Short form

When there is any ambiguity, use this rule:

> **Never let an Omnigent instance upgrade itself. O1 upgrades O2; O2 upgrades
> O1. The healthy peer stays alive and supervises the entire operation.**
