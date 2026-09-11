# Total managed telemetry budget

## Writer integration and remaining architecture boundary

The current retention/status helper reads the archive allocation from the shared
policy (32 GB). `managed_diagnostics.py`, wired into the existing launchd jobs,
passes policy-derived backup targets to the unchanged file exporters: active
segments are included and one eighth of each tier is withheld from the nominal
rotation target. These targets are NOT admission. Legacy files, protected files
and failed asynchronous cleanup can exceed them. `managed_storage.state` remains
`INCOMPLETE` independently of capture/processor health.

The diagnostic supervisor owns pipes for both child descriptors and drains them
even when disk logging pauses. Every record opens/closes its destination after
synchronous rotation. All files in the Collector log directory, including legacy
ones, are charged against three quarters of the log allocation; the provenance
directory uses the remaining quarter. A reserved status slot records cumulative
dropped-byte counts across restarts. Missing/stale status is unavailable. No
pathname rotation is asserted to control a child's old open descriptor.

SQL backup callers must supply the complete `budget_root`, containing their slot
and sibling adoption/rollback evidence. They acquire the same adoption lock and
count the entire root. Retention's bounded last-run record also joins this lock.
Retention detail lists are capped at 128 entries, with aggregate counts retained.

Actual pinned fileexporter pressure reproduction (temporary storage only):

```sh
.venv/bin/python deploy/control-room/otel/pressure_fileexporter.py --collector /absolute/path/to/otelcol-contrib
```

On macOS, this holds closed fixture segments immutable against the actual
asynchronous remover. The pinned v0.159.0 Collector exceeded its nominal active
plus backup allowance while continuing to accept writes. This is negative
enforcement evidence, not a passing hard-cap test. The smallest strict archive
alternative is a synchronous budget-aware exporter/write sink with explicit
backpressure, while retaining the existing processors, identity and archive.

The installed Codex executable itself writes optional `CODEX_ROLLOUT_TRACE_ROOT`
bundles. These are distinct from required `sessions/.../rollout-*.jsonl` and the
operational state SQLite database. The public implementation inspected at
`openai/codex@02a8f038b87ad34d4a1dc5058eda26972ed7aa6c` uses a per-bundle mutex,
direct payload file creation and a buffered open trace descriptor. It exposes no
cross-process allocation or record cap through that interface. This public
source corroborates the observed writer; it is not a claimed reproducible build
provenance for the installed Desktop binary. `history.max_bytes` is unrelated.
A safe strict native cap needs upstream writer support or a separately approved
storage boundary. Do not patch the bundled binary or limit required session I/O.

Capture deletion remains paused: a Stop hook and an idle/lsof snapshot do not
prevent a native session from resuming or appending a retained reference during
deletion. Native tracing and retained-evidence owners do not participate in the
deletion transaction. Unknown references remain protected. No synthetic reference
callback is presented as live coordination, and zero old metadata candidates is
a valid outcome.

The sections below describe the earlier sidecar checkpoint; the current writer
coverage and machine adoption evidence remain in the paired issues.

`managed_storage_policy.json` is the owner-approved **target** for the next
storage integration. It does not change the installed archive-only policy merely
by being present. The recovered provenance sidecar now consumes component
allocations; native rollout and Collector activation remain separate gates. `managed_budget.py` implements pure budget assessment and old
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
The provenance sidecar uses fixed artifact/database/log bounds from the shared
policy. The native rollout writer and Collector do not participate in its lock.
The archive-only allocation has NOT been retired or activated as a total budget.

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


## Bounded provenance continuation

`codex_otel_decisions.py` is the recovered installed sidecar, extended in this
active repository. Its original bytes and existing maintenance/hook wiring were
preserved locally before editing. Original SHA-256:
`89b9ea335108193337e2e01f1743498234cb1dcfdaa0b83577cc2f441a5020b0`.
Exact filename/content searches across local Git/worktrees, relevant all-ref
histories and the published owner's code index did not locate a tracked owning
copy. The separate `TelemetrySelection` helper is a different implementation;
its inactive database remains protected and counted. No retired checkout is a
runtime dependency. This is installed-source recovery, not an invented upstream
commit attribution.

The `allocations` in the shared policy sum to 48,768,000,000 bytes, leaving
1,232,000,000 bytes unassigned inside the total. They are candidate component
ceilings, not proof that every writer honors them. The 10-GiB combined periodic
capture target fits within the 12-GB capture allocations; the difference is
bounded-growth headroom, not another allowance outside the total. Forensic's
4-GB sublimit remains included in the archive allocation. Age ceilings remain
60/3/30 days; protected or unverified references can prevent convergence.

The sidecar now uses:

- Nonblocking process locks, fresh owned-artifact scans and pre-write scratch
  admission. Every capture record/archive is limited to 8 MiB. Existing frozen
  files, allocation rounding and abandoned temporary files consume its 6-GB
  artifact allocation. Unknown scans pause optional artifact writes.
- A 1-GB database component: main DB at most 250 MB, WAL allowance 500 MB,
  remaining 250 MB reserved for SHM, allocation rounding and maintenance margin.
  Each cooperating transaction disables cache spilling, bounds input size,
  checks the worst full-database WAL generation and pauses on pinned readers.
  All hook/maintenance/freeze commands share the existing database writer lock.
  No DB/WAL file is manually truncated, unlinked or replaced.
- A dedicated maintenance log with 1-MiB segments and three backups, a 64-KiB
  record cap and descriptor close/reopen on each record. The existing launchd
  stdout/stderr logs are preserved. Collector logs remain a coverage gap.
- A metadata dry-run in the existing maintenance job. It refuses unknown active,
  lean-evidence and rollback references. The deletion adapter rechecks every
  predicate inside one transaction and requires an unchanged tested backup;
  changed rows are retained. No live reference-clearance adapter is asserted.
- One bounded backup slot with an isolated SQLite backup/restore, integrity,
  foreign-key and logical-content check. Existing backup/interrupted slots are
  retained and require review instead of automatic replacement. There are no
  per-row tombstones.

The recovered timer's old capture deletion used neither reliable native activity
nor rollback reference checks. Its deletion phase now reports a pause while
those references are unknown. It no longer manufactures an observed completion
from transcript idle time. This preserves evidence, but leaves capture age/size
convergence OPEN; the timer is not a substitute for native writer admission.

`adopt_macos.adopt_provenance` extends the existing adoption module. It requires
clean committed source, the exact preserved installed hash and bounded backup
headroom. It replaces dependency modules before the hook entrypoint, preserves
source rollback and leaves the database, existing jobs, Collector and app alone.
Rollback verifies adopted hashes before restoring the original hook and removing
only the exact newly installed modules. It refuses to overwrite newer edits.

### Remaining activation gates

No verified native rollout pre-write limit was found in the installed binary's
local source/configuration surface. That writer does not share the sidecar lock.
Collector file rotations still use the old independent archive allocation;
Collector log descriptors and the old generic adoption path also need bounds.
Do not declare the 50-GB total enforced, switch to a green aggregate status or
retire the old policy until those real writers are integrated. Do not restart
active clients to apply an unverified workaround. Full Desktop/login persistence
remains a separate safe-window gate.

Run the isolated suite with the existing job's interpreter as well as the
repository interpreter:

```sh
/usr/bin/python3 -B -m unittest discover -s deploy/control-room/otel -p test_managed_storage.py
.venv/bin/python -B -m unittest discover -s deploy/control-room/otel -p 'test_*.py'
```

`managed_storage.py /absolute/path/to/roots.json` performs read-only combined
accounting. It intentionally exits 2 with `INCOMPLETE`: complete inode measurement
is not complete writer coverage. Its protected-byte figure conservatively counts
all bytes; it never implies they are all frozen or eligible for deletion. Local
root manifests and raw evidence belong outside Git and inside accounted storage.
