# Managed Mac telemetry storage

The authoritative operational policy is [managed-target.md](managed-target.md).
The single 50,000,000,000-byte managed upper target covers every explicit managed
root. It is not a mathematical quota; ordinary Codex/Omnigent execution and
required conversation/session persistence continue at every pressure state.

## Installed control path

`managed_storage.management_status` reads the reviewed local
`state/managed-roots.json` manifest, counts each inode once using the larger of
logical and allocated bytes, and persists hysteresis in a bounded
`state/managed-target-state.json` record. These files are inside counted storage.
The existing retention job refreshes it every 300 seconds; installed status and
provenance maintenance also refresh it. No additional scheduler or database exists.

Hooks read that state without scanning the archive. A missing, corrupt, stale
(over 360 seconds), or policy-mismatched record pauses optional capture. At
48 GB and above, or while recovering above 46 GB, hooks return protocol success
without starting optional provenance capture or its SQLite transaction. Required
Codex transcripts, session databases and resume state are separate and untouched.
The existing provenance reconciler also skips optional enrichment while paused.

The diagnostic supervisor keeps draining child stdout/stderr through its pipes
while optional disk logging is paused, and records dropped-byte counts. It never
stalls the Collector or makes child exit status depend on diagnostic capacity.
Essential bounded control/gap records remain writable during optional pause.

Skipped hooks increment a bounded counter; the gap remains visible after capture
resumes. Counts are lower bounds: lock or disk failure can prevent recording an
individual skip. Missing/stale diagnostic status is unavailable, not a zero.
Unexpected optional artifact failures still appear in the existing provenance
row's completeness/error fields. Silent producer loss remains unknown.

## Cleanup and evidence protection

At 45 GB, the existing retention job lowers its archive cleanup target by the
observed aggregate excess above 45 GB, within the existing archive allocation.
It applies only the existing closed-segment candidates and age rules; active,
unrecognized and changed files are preserved. A failed unlink remains an error.
A completed scan with protected space remaining reports `target_pending`, not a
quota-policy failure. `managed_cleanup_blocked` makes remaining pressure visible.

Age ceilings remain lean 60 days, forensic 3 days, auxiliary capture 30 days and
eligible completed metadata 30 days. Size pressure may reclaim eligible archive
segments sooner. Frozen, in-flight and required rollback evidence remains counted
and protected. Inventory's conservative protected-byte count is not a declaration
that every byte is frozen, and gives no deletion authority.

Metadata deletion still requires explicit completion older than 30 days, pruned
captures, and confirmed absence of frozen, active, retained-lean and rollback
references inside the existing deletion transaction, with a tested consistent
backup. Unknown references remain protected. Native writers do not share a
reference/activity fence, so the unsafe legacy capture deletion phase remains
paused. Zero eligible rows is valid; no blanket historical purge is authorized.

## Component bounds and external limitations

Policy allocations sum to 48,768,000,000 bytes inside the total; the remaining
1,232,000,000 bytes is unassigned margin. The 4-GB forensic sub-target belongs
inside the 32-GB archive allocation. Nominal Collector rotations leave additional
headroom inside that allocation. Artifact scratch admission, bounded SQLite/WAL
transactions, descriptor-aware diagnostic rotation and source-adoption backup
reservations remain in place as cooperating component safeguards.

The pinned Collector file exporter and native optional raw-trace writer have no
shared synchronous pre-write quota or safe live native pause interface. The real
isolated Collector experiment measured 9,584,640 bytes against a nominal
2,097,152-byte rotation allowance when asynchronous cleanup failed. Installed
status retains that evidence and reports current aggregate overshoot separately;
it does not attribute current bytes to a writer without evidence.

Unknown external in-flight bytes remain null. Management decisions use observed
disk pressure, explicitly excluding unknowable future reservations. APFS clones,
snapshots and unrelated storage are outside a machine-wide physical-quota claim.
`hard_ceiling_enforced=false` is intentional and not an acceptance blocker.

## Adoption and reproduction

The recovered provenance sidecar's original preserved SHA-256 was
`89b9ea335108193337e2e01f1743498234cb1dcfdaa0b83577cc2f441a5020b0`.
The separate legacy TelemetrySelection store remains preserved and counted.
Use the existing clean-source `adopt_macos` flow, reviewing current installed
hashes and keeping its bounded rollback copies. It updates dependencies before
short-lived entrypoints and preserves newer O3 sources. Activating new diagnostic
supervisor code needs a single guarded Collector restart in an idle window;
short-lived retention/provenance jobs pick up the source at their next run.

```sh
.venv/bin/python -B -m unittest discover -s deploy/control-room/otel -p 'test_*.py'
/usr/bin/python3 -B -m unittest discover -s deploy/control-room/otel -p 'test_managed_target*.py'
.venv/bin/python -B deploy/control-room/otel/pressure_managed_target.py --python /usr/bin/python3
/usr/bin/python3 -B "$HOME/Library/Application Support/ControlRoom/otel/bin/control_room_otel.py" status --json
```

The pressure exercise uses a 50-MB target and real hook subprocesses, files,
SQLite and archive cleanup, never live managed roots. It proves pause, gap counts,
hysteresis, recovery and protected-file preservation. Actual installed Codex
completion/resume and installed O3/shared-lean checks are separate runtime proof
recorded in the private companion issue. Desktop login/reboot persistence and
CI/DCO/maintainer approval remain separate gates.
