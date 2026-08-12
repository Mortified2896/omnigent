# Control Room dual-instance upgrade safety

This document is the authoritative safety contract for upgrading
Omnigent instances in the Control Room. It overrides all less-specific
deployment wording in the repository.

## Hard invariant

> Never let an Omnigent instance upgrade itself. O1 upgrades O2;
> O2 upgrades O1. The healthy peer stays alive and supervises the
> entire operation.

This is the elegant expression of the rule. An instance NEVER upgrades
itself. The healthy peer supervises the entire operation.

## Required TARGET / SUPERVISOR declaration

Before any deployment action, the operator or tooling must explicitly
state:

```text
TARGET = O1   (or O2)
SUPERVISOR = O2   (or O1)
```

and the two must be different instances. The valid pairs are:

  * target = O2, supervisor = O1
  * target = O1, supervisor = O2

The invalid pairs are:

  * target = O1, supervisor = O1
  * target = O2, supervisor = O2

If the operator accidentally supplies an invalid pair, the deployment
tool MUST refuse to run. The `peer_deployer` module's
`identity.require_distinct()` helper enforces this at the API and CLI
level.

## Target / supervisor identity

The two instances are defined canonically in
`deploy/scripts/peer_deployer/identity.py`:

```python
O1 = Instance(
    name="O1",
    deployment_root=Path("/opt/omnigent"),
    service_unit="omnigent.service",
    host_unit="omnigent-host.service",
    port=4097,
    health_url="http://127.0.0.1:4097/health",
)

O2 = Instance(
    name="O2",
    deployment_root=Path("/opt/omnigent-production"),
    service_unit="omnigent-production.service",
    host_unit="omnigent-production-host.service",
    port=4197,
    health_url="http://127.0.0.1:4197/health",
)
```

The roots, ports, service units, and host units MUST be distinct.
The `peer_deployer` library refuses to run if any of these collide.

## Transaction identity

Every mutable phase of a deployment is preceded by a transaction
record. The format is:

```text
promotion-<YYYYMMDDTHHMMSSZ>-<8-hex-chars>
```

The transaction record is a JSON file at:

```text
/var/lib/omnigent-control-room/transactions/<tx_id>/transaction.json
```

The record captures:

  * target and supervisor identity
  * the accepted artifact SHA, version, and wheel SHA-256 hashes
  * the old runtime path, SHA, and version
  * the new (accepted) runtime path, SHA, and version
  * the DB backup path, SHA-256, and integrity status
  * the old and target DB schema
  * the current phase
  * the list of resources created by this transaction
  * whether the transaction has crossed a mutation boundary

The mutation boundary is the moment the deployment tool first writes
to a target-owned path. The flag is set ONCE and never cleared. The
rollback subsystem refuses to operate on any transaction that has not
crossed the mutation boundary.

## Preflight gate

Before any destructive phase, the deployment tool MUST run a strict
preflight that verifies:

  * target != supervisor
  * target service unit is known to systemd
  * target host unit is known to systemd
  * supervisor service unit is known to systemd
  * supervisor host unit is known to systemd
  * supervisor is healthy (server active, host active, /health OK)
  * supervisor is running the EXACT accepted artifact (SHA + version)
  * the accepted artifact exists at the supervisor's release root
  * the main wheel SHA-256 matches the accepted value
  * the SDK client wheel SHA-256 matches the accepted value
  * the SDK UI wheel SHA-256 matches the accepted value
  * the artifact's runtime identity matches the accepted SHA + version
  * the target DB exists and passes integrity check
  * the rollback location is writable
  * disk space is sufficient
  * the host-level deployer is available
  * no other deployment transaction is in flight
  * all required scripts are present and executable
  * the service-state helper works correctly

If any check fails, the preflight MUST exit non-zero without:

  * stopping target services
  * renaming / deleting / copying the active runtime
  * touching the DB
  * invoking rollback
  * mutating the supervisor

The preflight is implemented in `deploy/scripts/peer_deployer/preflight.py`.

## Service-state helper

The system uses `systemctl is-active` to determine service state.
`systemctl is-active` returns a non-zero exit code when a service is
NOT active, which causes the broken
`systemctl is-active ... | grep -q '^inactive$'` pattern to fail
under `set -o pipefail`.

The vetted helper is `peer_deployer.service_state` and the bash
library `peer_deployer_lib.sh`. The helper captures `systemctl
is-active` output to stdout and IGNORES the exit code. State
classification is performed on the captured token.

The helper must distinguish:

  * `active`
  * `inactive`
  * `failed`
  * `activating` / `reloading` (acceptable but not yet "active")
  * unknown (the unit is not installed)

The regression tests in
`tests/deploy/test_peer_deployer_service_state.py` exercise the
broken pattern under `pipefail` and prove the vetted helper does not
have the same bug.

## Rollback

Rollback is restricted to resources that a transaction has explicitly
recorded as owned. The rollback subsystem refuses to delete or rename
any path that is not listed in the transaction's `owned_resources`.

The hard rule is:

> A failure before the first committed deployment mutation must be
> incapable of deleting, renaming, replacing, restoring, or otherwise
> changing the current active runtime.

The 2026-08-08 incident violated this rule. The previous attempt
inferred "active runtime = new release" from path shape and deleted it
even though no part of the aborted promotion had created a new
runtime. The fix is twofold:

  1. Before any destructive phase, the deployment tool creates a
     transaction record with `mutation_boundary_crossed = False`.
  2. The rollback subsystem refuses to operate on any transaction
     that has not crossed a mutation boundary.

In addition, the rollback subsystem enforces:

  * the rollback must verify the DB backup integrity before any
    application work
  * the rollback must pair application and DB restore atomically
  * the rollback must preserve rollback artifacts (backup, release
    dirs, transaction record) after rollback
  * the rollback runs at most once per failed transaction

## Privileged host deployer

The O2 runtime is sandboxed with `NoNewPrivs=1`, `CapEff=0`, read-only
host paths, and no SSH to host. To upgrade the O1 instance, O2 must
invoke a narrowly scoped host-level deployer that runs OUTSIDE the
sandbox.

The deployer requirements are:

  * narrowly scoped: only the operations needed for Omnigent
    deployment and recovery
  * inputs are validated (target, supervisor, exact artifact SHA)
  * deployer cannot target the supervising instance
  * operation logs are durable
  * O2 can observe progress/result via the transaction record
  * no arbitrary shell / root command passthrough
  * does not weaken NoNewPrivs

The deployer signature is one of:

  * `peer_deployer promote --target O1 --supervisor O2`
  * `peer_deployer rollback --tx-id <tx> --target O1 --supervisor O2`

The deployer REFUSES to run if:

  * target == supervisor
  * supervisor is not verified to be the exact accepted artifact
  * the preflight fails
  * the exact artifact SHA does not match the expected value

## Host deployer roles

The host-level deployer is responsible for the following O1 mutations
during a promotion:

  1. snapshot the target's current state
  2. create a verified DB backup
  3. create a transaction record
  4. stage the exact accepted artifact
  5. verify the staged runtime
  6. stop the target's services
  7. migrate the target's DB
  8. switch the target's runtime symlink
  9. start the target's server
 10. wait for health
 11. start / reconnect the target's host
 12. run focused acceptance
 13. mark the transaction committed
 14. preserve rollback artifacts

The host-level deployer is NOT responsible for:

  * upgrading the supervisor (O2)
  * replacing the supervisor's runtime
  * touching the supervisor's DB
  * disabling the supervisor's sandbox
  * granting the O2 runtime broad root privileges

## Required regression coverage

The following six scenarios MUST be covered by focused tests:

  1. preflight fails before mutation → active runtime untouched
  2. candidate staging fails → active runtime untouched
  3. DB migration fails after candidate staging → paired rollback
  4. service start fails after switch → paired rollback
  5. rollback receives unknown/unowned path → refuse deletion
  6. self-upgrade target == supervisor → refuse before mutation

These are covered by `tests/deploy/test_peer_deployer_rollback.py` in
the `TestSixRequiredScenarios` class.

## Required additional checks

  * self-upgrade rejection
  * target/supervisor validation
  * preflight zero-mutation failure
  * transaction ownership
  * rollback path ownership
  * DB/application paired rollback
  * `systemctl` inactive-state bug (`pipefail` + `is-active`)
  * exact-artifact identity (main wheel + SDK wheels + runtime SHA)
  * O2 supervisor zero-drift guard
