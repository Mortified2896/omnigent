# Legacy Control Room dual-instance upgrade safety

This document is the authoritative safety contract **only for the legacy
Control Room two-peer `peer_deployer` mechanism when two distinct Omnigent
instances have deliberately been provisioned and that mechanism has explicitly
been selected**.

It is **not** the current HomeLab deployment policy for the RTX Omnigent
runtime. The current HomeLab topology has one active RTX O1 under external Mac
Codex control and no standing live O2 peer. Old O1/O2 on `ai-control-hub` are
retired from active service and archived.

Therefore:

- do not start, restore, recreate, or otherwise reactivate old O2 merely to
  satisfy this document or `peer_deployer` preflight;
- do not fabricate or relabel a supervisor identity;
- if `peer_deployer` is selected while no deliberately provisioned second peer
  exists, treat that deployment mechanism as inapplicable and refuse it rather
  than changing topology to make it pass;
- reintroducing a second Omnigent peer requires explicit owner authorization;
- current RTX deployment decisions are governed by the current HomeLab
  `docs/omnigent-current-topology.md` and `docs/codex-server-workflow.md`, with
  the Mac Codex app as external controller and the rollback-preserving RTX
  release path.

Historical migration text that says RTX O1 "must" use O2 supervision is
superseded for present-state deployment decisions. This document overrides
less-specific wording only **inside its scoped legacy two-peer mechanism**; it
does not override the current topology authority above.

## Hard invariant within the legacy two-peer mechanism

> Never let an Omnigent instance upgrade itself. O1 upgrades O2;
> O2 upgrades O1. The healthy peer stays alive and supervises the
> entire operation.

This is the core invariant when a real two-peer topology exists. An instance
never upgrades itself, and the healthy peer supervises the operation.

For the current single-primary RTX topology, preserve the corresponding safety
properties without fabricating a peer: external control, exact tested artifact
identity, consistent backups, reversible release switching, runtime/health
verification, and rollback preservation. The running O1 must not autonomously
replace itself from inside its own task context.

## Required TARGET / SUPERVISOR declaration

This section applies only after the legacy two-peer mechanism has been validly
selected and two distinct peers actually exist.

Before any deployment action through that mechanism, the operator or tooling
must explicitly state:

```text
TARGET = O1   (or O2)
SUPERVISOR = O2   (or O1)
```

and the two must be different instances. The valid pairs are:

- target = O2, supervisor = O1
- target = O1, supervisor = O2

The invalid pairs are:

- target = O1, supervisor = O1
- target = O2, supervisor = O2

If the operator accidentally supplies an invalid pair, the deployment tool MUST
refuse to run. The `peer_deployer` module's `identity.require_distinct()` helper
enforces this at the API and CLI level.

Absence of a peer is not an invitation to restore one. It means this mechanism
is not currently usable.

## Target / supervisor identity

The legacy instances are defined canonically in
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

These identities describe the legacy dual-instance deployment contract; they do
not define the current RTX VM 100 runtime layout. The roots, ports, service
units, and host units MUST be distinct whenever the legacy mechanism is used.
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
evidence; disk headroom; timestamp; and non-secret builder/operator identities.
`acceptance_record_sha256` hashes canonical JSON with that field omitted. The
complete record is exclusive-created and never replaced. Validation rechecks
immutable resources, runtime identity, imports, package locations/versions,
`uv pip check`, and boot evidence.

These acceptance properties remain useful for the RTX release path even though
the peer relationship itself is not part of the current topology.

## Deployment modes

The following modes belong to the legacy two-peer mechanism:

- `bootstrap-first-peer` activates the already booted, complete candidate at
  the target immutable `releases/<sha>` path. The healthy supervisor need not
  run the candidate yet. Bootstrap never copies the old supervisor closure.
- `peer-copy` proves the supervisor runs the exact accepted SHA/version and
  copies that accepted release to the other peer.

Common, bootstrap, and peer-copy preflight checks are distinct and named. No
mode has a force or skip-check interface.

Do not select either mode for the current RTX single-primary topology merely to
reuse its code path. Use the current RTX rollback-preserving deployment path.

## Transaction identity

Every mutable phase of a legacy peer deployment is preceded by a transaction
record. The format is:

```text
promotion-<YYYYMMDDTHHMMSSZ>-<8-hex-chars>
```

The transaction record is a JSON file at:

```text
/var/lib/omnigent-control-room/transactions/<tx_id>/transaction.json
```

The record captures:

- target/supervisor identity and explicit deployment mode
- acceptance-record path and canonical payload SHA-256
- artifact SHA/version, frontend tree hash, and exact wheel filenames/hashes
- supervisor SHA/version, server/host PIDs, and active-entry timestamps
- the old runtime path, SHA, and version
- the new (accepted) runtime path, SHA, and version
- the DB backup path, SHA-256, and integrity status
- the old and target DB schema
- the current phase
- resources created and rollback-owned by this transaction
- separately, pre-existing referenced resources that rollback must preserve
- whether the transaction has crossed a mutation boundary

The active-state mutation boundary is the first write that can change the
active target runtime, DB, metadata, or services. Transaction-owned staging may
be created before it; a pre-boundary failure may remove only that exact owned
staging path. The flag is set once and never cleared. Paired rollback refuses
any transaction that has not crossed the boundary.

## Preflight gate

Before any destructive phase of the legacy two-peer mechanism, the deployment
tool MUST run a strict preflight that verifies:

- target != supervisor
- target service unit is known to systemd
- target host unit is known to systemd
- supervisor service unit is known to systemd
- supervisor host unit is known to systemd
- supervisor is healthy (server active, host active, `/health` OK)
- `mlflow-storage-guard.timer` is active and its critical latch is absent
- the immutable acceptance record and embedded digest are valid
- for peer-copy, supervisor runs the exact accepted SHA/version and has it at
  the supervisor release root
- for bootstrap, the accepted candidate is complete at the target immutable
  release root and is not supervisor-owned
- the main wheel SHA-256 matches the accepted value
- the SDK client wheel SHA-256 matches the accepted value
- the SDK UI wheel SHA-256 matches the accepted value
- runtime identity, installed paths/versions, frontend tree, `uv pip check`, and
  boot evidence match acceptance
- the target DB exists and passes integrity check
- the rollback location is writable
- disk space is sufficient
- the host-level deployer is available
- no other deployment transaction is in flight
- all required scripts are present and executable
- the service-state helper works correctly

If any check fails, the preflight MUST exit non-zero without:

- stopping target services
- renaming / deleting / copying the active runtime
- touching the DB
- invoking rollback
- mutating the supervisor

In the current single-primary RTX topology, expected failure on missing legacy
peer units/health means **do not use this mechanism**. It does not authorize
starting/restoring O2.

The preflight is implemented in `deploy/scripts/peer_deployer/preflight.py`.

## Service-state helper

The system uses `systemctl is-active` to determine service state.
`systemctl is-active` returns a non-zero exit code when a service is NOT active,
which causes the broken `systemctl is-active ... | grep -q '^inactive$'` pattern
to fail under `set -o pipefail`.

The vetted helper is `peer_deployer.service_state` and the bash library
`peer_deployer_lib.sh`. The helper captures `systemctl is-active` output to
stdout and ignores the exit code. State classification is performed on the
captured token.

The helper must distinguish:

- `active`
- `inactive`
- `failed`
- `activating` / `reloading` (acceptable but not yet "active")
- unknown (the unit is not installed)

The regression tests in `tests/deploy/test_peer_deployer_service_state.py`
exercise the broken pattern under `pipefail` and prove the vetted helper does
not have the same bug.

## Rollback

Rollback is restricted to resources that a transaction has explicitly recorded
as owned. The rollback subsystem refuses to delete or rename any path that is
not listed in the transaction's `owned_resources`.

The hard rule is:

> A failure before the first committed deployment mutation must be incapable of
> deleting, renaming, replacing, restoring, or otherwise changing the current
> active runtime.

The 2026-08-08 incident violated this rule. The previous attempt inferred
"active runtime = new release" from path shape and deleted it even though no
part of the aborted promotion had created a new runtime. The fix is twofold:

1. Before any destructive phase, the deployment tool creates a transaction
   record with `mutation_boundary_crossed = False`.
2. The rollback subsystem refuses to operate on any transaction that has not
   crossed a mutation boundary.

In addition, the rollback subsystem enforces:

- the rollback must verify the DB backup integrity before any application work
- the rollback must pair application and DB restore atomically
- the rollback must preserve rollback artifacts (backup, release dirs,
  transaction record) after rollback
- the rollback runs at most once per failed transaction

These rollback principles should also be preserved in the current RTX path even
where the peer-specific transaction schema does not apply directly.

## Legacy trusted administrative agents and privileged host deployer

This section records capability for the historical two-peer deployment only. It
must not be used to infer current RTX privileges.

The legacy O1 and O2 agent hosts were intentionally authorized for
passwordless root through `sudo -n` under their reviewed machine policy. That
capability never relaxes the two-peer invariant when the legacy mechanism is in
use.

The repository-managed historical Control Room source for that policy is
`deploy/control-room/trusted-root/`. Its host drop-ins, non-secret env fragments,
and sudoers fragment were installed only through its explicit installer path;
generic Omnigent installation did not enable it automatically.

For current RTX operations, privilege is established only by current machine
policy and fresh observation. Use the normal `hermes` account and `sudo -n` only
for narrowly scoped operations after verifying it is available and required.
Do not restore legacy trusted-root configuration merely because this historical
contract records it.