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

## Immutable candidate acceptance

Artifact identity is never compiled into active deployer source. Promotion takes
an explicit immutable acceptance record at:

```text
/var/lib/omnigent-control-room/accepted-artifacts/<source-sha>/acceptance.json
```

Schema v1 binds source SHA/package version; exact wheel filenames and hashes;
the frontend tree hash; immutable release/runtime paths; installed paths and
versions; successful `uv pip check`; embedded build SHA; isolated temporary-port
boot, health, `/v1/info` version (and build SHA when exposed), and HTML/assets
evidence; disk headroom; timestamp;
and non-secret builder/operator identities. `acceptance_record_sha256` hashes
canonical JSON with that field omitted. The complete record is exclusive-created
and never replaced. Validation rechecks immutable resources, runtime identity,
imports, package locations/versions, `uv pip check`, and boot evidence.

## Deployment modes

Every promotion declares one mode:

* `bootstrap-first-peer` activates the already booted, complete candidate at
  the target immutable `releases/<sha>` path. The healthy supervisor need not
  run the candidate yet. Bootstrap never copies the old supervisor closure.
* `peer-copy` proves the supervisor runs the exact accepted SHA/version and
  copies that accepted release to the other peer.

Common, bootstrap, and peer-copy preflight checks are distinct and named. No
mode has a force or skip-check interface.

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

  * target/supervisor identity and explicit deployment mode
  * acceptance-record path and canonical payload SHA-256
  * artifact SHA/version, frontend tree hash, and exact wheel filenames/hashes
  * supervisor SHA/version, server/host PIDs, and active-entry timestamps
  * the old runtime path, SHA, and version
  * the new (accepted) runtime path, SHA, and version
  * the DB backup path, SHA-256, and integrity status
  * the old and target DB schema
  * the current phase
  * resources created and rollback-owned by this transaction
  * separately, pre-existing referenced resources that rollback must preserve
  * whether the transaction has crossed a mutation boundary

The active-state mutation boundary is the first write that can change the
active target runtime, DB, metadata, or services. Transaction-owned staging
may be created before it; a pre-boundary failure may remove only that exact
owned staging path. The flag is set ONCE and never cleared. Paired rollback
refuses any transaction that has not crossed the boundary.

## Preflight gate

Before any destructive phase, the deployment tool MUST run a strict
preflight that verifies:

  * target != supervisor
  * target service unit is known to systemd
  * target host unit is known to systemd
  * supervisor service unit is known to systemd
  * supervisor host unit is known to systemd
  * supervisor is healthy (server active, host active, /health OK)
  * `mlflow-storage-guard.timer` is active and its critical latch is absent
  * the immutable acceptance record and embedded digest are valid
  * for peer-copy, supervisor runs the exact accepted SHA/version and has it at
    the supervisor release root
  * for bootstrap, the accepted candidate is complete at the target immutable
    release root and is not supervisor-owned
  * the main wheel SHA-256 matches the accepted value
  * the SDK client wheel SHA-256 matches the accepted value
  * the SDK UI wheel SHA-256 matches the accepted value
  * runtime identity, installed paths/versions, frontend tree, `uv pip check`,
    and boot evidence match acceptance
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

## Trusted administrative agents and privileged host deployer

The O1 and O2 agent hosts are intentionally authorized for unrestricted,
passwordless root through `sudo -n`. Agents should use it autonomously for
authorized Control Room administration rather than asking the user to SSH in
or run privileged commands. Root access does not relax the hard invariant:
an agent must never upgrade its own instance, and the healthy peer must remain
available as supervisor.

The repository-managed Control Room source of truth for this policy is
`deploy/control-room/trusted-root/`. Its two host drop-ins, dedicated
non-secret env fragments, and sudoers fragment are installed by its explicit
`install.sh --install` path; generic Omnigent installation does not run it.
Both host environments therefore set `OMNIGENT_TRUSTED_ROOT_ACCESS=true`. The
installer validates the generated sudoers fragment with `visudo -cf` before
committing it and clears inherited syscall/capability restrictions, including
O2's `SystemCallArchitectures=native` setting.

At runtime Omnigent performs a bounded `sudo -n true` probe once per process
and only adds the trusted-root framework instruction when both the flag and
capability are present. A configured-but-failing probe is reported as a
capability mismatch and does not advertise root to the model. Generic
deployments remain unaffected because the flag defaults off.

A peer-supervised upgrade continues to use the narrowly scoped host-level
deployer and its explicit target/supervisor validation.

The deployer requirements are:

  * narrowly scoped: only the operations needed for Omnigent
    deployment and recovery
  * inputs are validated (target, supervisor, exact artifact SHA)
  * deployer cannot target the supervising instance
  * operation logs are durable
  * the supervisor can observe progress/result via the transaction record
  * no arbitrary shell / root command passthrough through the deployer interface

The deployer signature is one of:

  * `peer_deployer promote --target O1 --supervisor O2 --mode <mode> --acceptance-record <path> --evidence-dir <path>`
  * `peer_deployer promote --target O2 --supervisor O1 --mode <mode> --acceptance-record <path> --evidence-dir <path>`
  * `peer_deployer rollback --tx-id <tx> --target <target> --supervisor <peer>`

The canonical host wrapper accepts the same explicit identities, mode,
acceptance record, and evidence path. Evidence must live below the root-owned
`/var/lib/omnigent-control-room/evidence/` tree. There is no force/skip interface.

The deployer REFUSES to run if:

  * target == supervisor
  * peer-copy supervisor is not verified as the exact accepted artifact
  * bootstrap candidate is not the already booted immutable accepted release
  * the preflight fails
  * the exact artifact SHA does not match the expected value

## Host deployer roles

The host-level deployer is responsible for the following target mutations
during a promotion:

  1. snapshot the target's current state
  2. create a transaction record
  3. create a verified DB backup
  4. reference the accepted bootstrap candidate or stage the peer-copy artifact
  5. verify the exact candidate runtime and immutable acceptance again
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

  * upgrading or replacing the declared supervisor
  * touching the supervisor's DB
  * granting agent root privileges (that is host configuration, independently
    gated by the runtime capability probe)

Before target mutation it records supervisor runtime SHA/version, both
MainPIDs, and both active-entry monotonic timestamps. It captures the same
fields after acceptance and rollback; drift prevents commit and is reported.

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
