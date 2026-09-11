# Total managed telemetry budget

`managed_storage_policy.json` is the owner-approved **target** for the next
storage integration. It does not change the installed archive-only policy merely
by being present. `managed_budget.py` implements pure budget assessment and old
metadata selection, not scanning, deletion, reservations or writer enforcement.
Do not report total-budget completion until the actual Mac writers are integrated.

## One envelope, distinct age limits

Read the numeric limits and age ceilings from the policy file. The total includes
both archive tiers, auxiliary captures, database/WAL/SHM, operational logs,
backups, temporary maintenance files, installed telemetry runtime and other
managed telemetry. Forensic bytes are part of `otel_archive`, not another
addition. The former separate capture allowance must fit INSIDE the total.
Frozen/in-flight/required rollback bytes count toward usage while remaining
protected from automatic deletion. Age ceilings are not minimum retention
promises when size-based cleanup applies.

## Snapshot contract

`assess_budget` requires all seven named components, an explicit complete flag,
a timezone-aware observation, and known outstanding reserved bytes. Each component
contains `bytes` and `protected_bytes`; the latter is a subset, not a deduction.
Missing, invalid, future or stale measurements reject new growth. Use explicit
zero only after confirming a component is absent, never after a failed scan.

The scanner/adapter must resolve explicit telemetry-owned roots, classify each
file once (including nested roots and hard links), reject unsafe paths and count
frozen content, WAL, backups and scratch files. Record logical and allocated size;
a conservative admission estimate may sum their per-file maximum. State exactly
what the measurement covers; APFS clones/snapshots and unrelated files are not a
machine-wide physical quota. Unknown/unreadable managed paths make coverage
incomplete. Never recursively sweep the user's whole home or application data.

`fits_snapshot` means only that used bytes + outstanding reservations + requested
maximum growth fit at that observation. It creates no reservation. The caller
must serialize the check and reservation with other writers, account for writes
since the snapshot, bound maximum records/transactions and release/reconcile
reservations on success, failure or recovery. Alternatively, partition writer
limits with proved maximum overshoot/reserves inside the same envelope. A timer
or two independent writers reading a green snapshot cannot guarantee a limit.
No existing collector, LaunchAgent or provenance writer calls this module yet.

## Authorized metadata cleanup

`metadata_cleanup_decision` is a pure predicate: explicit completed state, age
strictly beyond the metadata horizon, confirmed pruned captures and explicit
absence of frozen/in-flight/retained-evidence/rollback dependencies are required.
Legacy format or age alone grants no deletion permission. The adapter must map
actual database states, not default missing fields to false, and recheck references
inside the deletion transaction. Keep metadata needed to interpret retained lean
traces even when an auxiliary capture is gone.

Use the existing maintenance job. Before first live deletion, preserve a bounded
consistent backup and test restore/foreign keys, row eligibility and page reuse.
Deleted SQL rows need not shrink the database file. `max_page_count` limits the
main database, not every accompanying file; `journal_size_limit` acts at journal
reset/checkpoint and is not an in-flight WAL ceiling. Pinned readers and large
transactions need separate bounded-write/backpressure handling. Never delete a
live DB/WAL file to reclaim space. Cleanup statistics must also be bounded; do
not replace removed history with an unlimited per-record tombstone table.

SQLite references: [PRAGMAs](https://www.sqlite.org/pragma.html) and
[WAL growth](https://www.sqlite.org/wal.html#avoiding_excessively_large_wal_files).

## Local integration still required

Locate/preserve the installed provenance writer's real source first. Reuse the
existing maintenance and capture paths rather than adding a scheduler/database.
Wire both policy consumers and status to the shared budget; retire independent
archive-only allocations at activation. Under pressure, refuse optional telemetry
growth and report the capture gap without stopping ordinary Codex/Omnigent tasks
or deleting protected evidence. Preserve the known-good capture/privacy/O3 paths.

The offline tests exercise decision logic only:

```sh
python3 -B -m unittest discover -s deploy/control-room/otel -p 'test_managed_budget.py' -v
```

Runtime scanner coverage, concurrent writers, descriptor-aware log rotation,
SQLite maintenance, bounded buffering, atomic adoption, rollback and native Mac
acceptance remain separate gates. Do not change `enforcement_verified: false`
into a completion claim based only on these tests.
