# Single-service Omnigent release controller

Status: design + pure contract + disposable executor prototype. No host/systemd
wiring exists, and nothing in this document authorizes a live rollout.

## Goal

The long-term deployment model is one canonical Omnigent service behind the
same stable URL. A user may ask Omnigent to change itself; the application can
prepare, test and review a custom commit, but an **external host-owned
controller** performs the privileged activation after receiving a narrow,
pinned request.

The active process never overwrites its own loaded code and never receives a
general root shell.

```text
user request
    |
running Omnigent prepares/tests/reviews exact custom SHA
    |
accepted immutable release
    |
external controller
    |
fence -> drain -> backup -> switch current -> restart -> verify
    |                                             |
    +---------------- rollback before writes -----+
```

## Target filesystem model

The intended host layout is deliberately independent of O1/O2 naming:

```text
/srv/omnigent-service/
  current  -> /srv/omnigent-releases/<sha>
  previous -> /srv/omnigent-releases/<previous-sha>
  state/                       # persistent service-owned state
  transactions/                # host-controller journals

/srv/omnigent-releases/<sha>/  # immutable accepted runtime
/srv/omnigent-artifacts/<sha>/ # acceptance evidence + build artifacts
```

Names and final paths are not yet an installation contract. HomeLab owns the
eventual host wiring.

## What the prototype implements

`deploy/scripts/release_controller/core.py` is the pure,
topology-independent contract. It has no filesystem, systemd, database,
network or model access. `executor.py` adds a disposable implementation that
rejects service roots outside the operating system's temporary directory.
It has no CLI, systemd adapter or application endpoint.

It defines:

- exact software identity: custom source SHA + acceptance digest;
- official/upstream provenance as optional display metadata, never inferred;
- fresh runtime pins: process generation, state identity and state generation;
- exact candidate evidence and rollback-from-current evidence;
- external-controller capabilities required before activation can be ready;
- a compare-and-swap activation request with bounded evidence lifetime;
- a durable phase vocabulary for the eventual host controller; and
- the critical rule that automatic state rollback is forbidden once writes have
  reopened.

The executor uses the current `acceptance-v2.json` record, canonical digest,
release file hashes and embedded SHA check. The v2 verifier is shared with the
existing RTX peer entrypoint; O1/O2 identity and target selection remain in the
peer controller. A disposable test may mirror a root-owned accepted record and
its immutable bytes into temporary roots, but the original record digest and
recorded release path remain pinned.

The executor journals one transaction record, serializes activation with a
nonblocking file lock, waits for stable zero active work after its adapter
confirms the write fence, backs up the instance's own SQLite state, atomically
switches `previous` and `current`, restarts through the fixed adapter, and
verifies before reopening writes. Its recovery path reconciles the old release
and `previous` pointer if power loss occurs between an atomic pointer update
and the following journal write.

The tests create the database with Omnigent's real Alembic migrations and use
the real SQLAlchemy conversation store. The executor adapter and its synthetic
v2 test releases still simulate the service process, ingress fence, work
counter and health checks. There is not yet a real Omnigent process adapter.
The executor is a learning prototype only; it is not a privileged controller
or an installation package.

A one-off RTX rehearsal also copied two existing root-accepted releases and
their `acceptance-v2.json` records into temporary roots, then used the executor
to stop a real Omnigent process at `4faf6943ee2735f67c7bebb922c443ef300b735e`,
back up its temporary database, switch the temporary pointer, and start
`30f919e08d459d6e74d1c0c1c0857bce7056d4e3` at the same loopback URL. Both
processes passed health, `/v1/info`, UI, build-SHA and schema checks; a seeded
conversation survived. The acceptance bytes were verified against their
original root-owned records. That run still simulated the write fence and
active-work counter and had no external clients. It proves the real package and
process transition path works in a quiescent disposable instance, not that the
host can fence or drain a live service safely.

## Required activation sequence

A future host adapter must implement the sequence below. The pure plan becoming
`ready` is not permission to skip any runtime recheck.

1. Recollect the pinned current/candidate evidence under one deployment lock.
2. Fence **new writes** while the existing process remains available for reads.
3. Drain in-flight work. Do not stop the service until all admitted work is
   known complete.
4. Recheck the current release, process generation, state identity/generation,
   and zero active work.
5. Create and verify a consistent backup of this service's own persistent state.
6. Durably record the transaction before changing the active release pointer.
7. Point `previous` at the verified old release and atomically move `current`
   to the exact accepted candidate.
8. Restart/re-exec the service from `current`.
9. Verify health, embedded source SHA, acceptance identity, schema and required
   UI/API behavior while writes remain fenced.
10. Commit activation, then reopen writes.
11. If failure occurs **before writes reopen**, automatic rollback may restore
    the verified previous release and backup.
12. If writes may have reopened, or rollback verification itself fails, stop
    automatic recovery and mark the transaction `recovery_required`.

The controller must survive the target process restart.

## Deliberate v1 restrictions

The first long-term implementation should remain conservative:

- same-schema releases only;
- one service only;
- no blue/green traffic routing;
- no Tailscale mutation during activation;
- no arbitrary browser-selected target/path/command;
- no force/skip-check endpoint;
- no model call in activation or routine rollback;
- no automatic rollback after writes reopen; and
- no removal of O1/O2 until this path has been rehearsed and deployed
  independently.

Schema-changing deployments need a separate migration protocol. An equal
Alembic head is necessary but is not by itself a proof of rollback
compatibility.

## Relationship to upstream `omni upgrade`

Upstream already supplies useful lifecycle ideas: drain active sessions, stop
old processes before swapping installed code, and start a fresh process on the
new version. Its package upgrader is not, by itself, an exact custom-SHA
production deployment mechanism.

The long-term controller should reuse upstream lifecycle primitives where they
are stable rather than fork them. The custom pieces we expect to retain are the
immutable accepted release identity, host-owned privilege boundary, exact-SHA
activation, persistent-state backup, and deterministic rollback policy.

The current session-drain helper is not safe to call directly from this
controller: its active-session query treats a query failure as zero work. The
server also has no server-wide write fence covering HTTP, WebSocket, runners
and scheduled work. The disposable adapter therefore treats fence, drain and
restart as explicit fail-closed operations. A real adapter must prove all
writers are fenced and admitted work is drained; a proxy-only gate would not
account for scheduled or in-process writers.

## Relationship to the O1/O2 work

The peer deployment remains the current production safety mechanism. The
single-service controller must be developed in a separate branch and initially
only against disposable instances.

Reusable concepts from the peer work include:

- acceptance records and byte verification;
- immutable release directories;
- transaction locking/journaling;
- runtime build identity;
- write fencing and draining;
- backup/rollback evidence; and
- the narrow external controller boundary.

O1/O2-specific supervisor identities must **not** leak into the generic
contract.

## Exit criteria before any live experiment

Do not replace the double setup until a disposable rehearsal proves all of:

- a real running Omnigent can request activation without possessing root;
- the external controller survives the Omnigent restart;
- already-admitted HTTP/WebSocket/runner/scheduled work is drained correctly;
- the existing acceptance-v2 record is used for both releases, and a real
  accepted Omnigent process is switched between them under an independently
  verified fence;
- the exact accepted candidate is launched, not rebuilt or pulled;
- service state and conversations survive a successful activation;
- startup failure restores the exact previous release and state while fenced;
- controller interruption produces deterministic recovery;
- a failure after writes reopen never restores stale state automatically; and
- the old O1/O2 path remains available as recovery during the trial period.

Only after repeated successful rehearsals should we discuss making the
single-service path the default or retiring O2.
