# Managed telemetry target

This supersedes the earlier hard-ceiling acceptance goal for local Mac telemetry.

The owner-approved behavior is a **50,000,000,000-byte managed upper target**,
not a mathematical never-exceed quota. Temporary overshoot is acceptable when an
external writer or delayed cleanup cannot stop synchronously. Ordinary Codex and
Omnigent work must never fail solely because optional telemetry storage is full.

## Pressure policy

The tracked policy uses hysteresis:

- below 45 GB: capture normally;
- at 45 GB: request eligible cleanup while capture continues;
- at 48 GB: pause cooperating optional telemetry and keep cleaning;
- resume paused optional telemetry only after usage falls to 46 GB or below;
- above 50 GB: report `OVER_TARGET`, keep normal Codex work running, and continue
  safe cleanup / optional-capture pause.

Protected, frozen, in-flight and required rollback evidence remains counted and
must not be silently deleted. If protected data prevents convergence, report the
condition and leave optional telemetry paused.

The existing age ceilings remain separate: lean OTel 60 days, forensic 3 days,
auxiliary captures 30 days, and eligible completed provenance metadata 30 days.
Size pressure can make eligible data leave earlier; these are age ceilings, not
minimum retention promises.

## Status semantics

`managed_budget.assess_budget` deliberately keeps the legacy `fits_snapshot` and
`OVER_BUDGET` fields for compatibility, but the authoritative operational fields
are `management_state`, `cleanup_requested`, and
`optional_telemetry_pause_requested`.

`normal_codex_allowed` is always true for storage-pressure decisions. An
incomplete/stale inventory pauses only cooperating optional telemetry; it must
not be surfaced as a reason to stop a Codex task.

The management states are:

- `NORMAL`
- `CLEANUP_DUE`
- `OPTIONAL_PAUSED`
- `OVER_TARGET`
- `INCOMPLETE`

`hard_ceiling_enforced` remains false. This is intentional.

## External writers

The pinned Collector file exporter and Codex native raw-trace writer do not
provide a shared synchronous pre-write quota interface. That is no longer a
completion blocker for this policy. Their known limitations must remain visible,
and their normal retention/rotation should be configured conservatively inside
the component targets.

When pressure crosses the optional-pause threshold, disable or skip optional
capture only where a supported safe boundary exists. Do not patch the bundled
Codex binary, disable required conversation/session persistence, or fail primary
task execution to protect observability storage.

The real acceptance criterion is practical convergence: telemetry usage stays
around the target over time, eligible cleanup runs, optional capture backs off
under pressure, gaps are reported truthfully, and normal Codex work continues.

## Acceptance

Before claiming this policy active on the Mac:

1. report one complete managed-root inventory;
2. wire the thresholds into status and cooperating writers/maintenance;
3. use isolated small-budget tests to demonstrate cleanup, pause, hysteresis, and
   recovery without breaking ordinary Codex execution;
4. preserve privacy, native IDs, capture identity and rollback;
5. keep `hard_ceiling_enforced=false` and document any external-writer overshoot.

A filesystem quota or custom native/exporter build is not required for this
managed-target policy.

## Optional gap attribution

`skipped_optional_hooks` remains a cumulative lower bound of observed skipped
operations, not lost turns. Repeated hooks within one turn can each increment
it. `optional_gap_reasons` contains fixed, bounded aggregate counters for
`managed_pause` (including stale inventory), `writer_lock_busy`,
`storage_allocation`, `sqlite_or_wal`, `reconcile_skipped`, `hook_failure`, and
`unknown`. The last gap timestamp and fixed reason are retained across refresh.
Historical counts without attribution remain `unknown`; they are never
retroactively assigned a cause. Counters saturate at the existing integer limit.

These counters cover the existing pause/failure paths plus failures caught by
the hook dispatcher. Partial artifact errors handled inside START/END capture,
silent producer loss, and a paused `maintain` reconciliation are not additional
counter events. Maintenance reports its pause separately. A control lock or
storage failure can prevent recording a gap; reporting then stays unavailable.
No prompts, paths, exception text, or event history enter the attribution state,
and attribution failure must not interrupt ordinary Codex execution.

Verify with `/usr/bin/python3 -B -m unittest discover -s deploy/control-room/otel
-p 'test_gap_attribution.py'` from the Mac feature worktree.
