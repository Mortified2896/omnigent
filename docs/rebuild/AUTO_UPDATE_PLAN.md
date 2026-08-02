# Auto-update recommendation

## 1. Compare stock `omni upgrade` vs the custom external updater

| Capability | Stock `omni upgrade` (v0.7) | Custom external updater (fork) | Verdict |
| --- | --- | --- | --- |
| **Update detection** | `omni upgrade --check` polls PyPI (`https://pypi.org/pypi/omnigent/json`) for the latest non-prerelease matching the installer's index. Returns a version delta. | `OmnigentUpdatable` CLI + server endpoints (`/v1/updater/...`). | **Stock sufficient** for "is there a newer release on PyPI?". The custom updater's API surfaces the same signal through the same client (the server is part of the same wheel) — but for an external install of stock Omnigent, `omni upgrade --check` is enough. |
| **Drain posture** | `--drain` (default): wait for in-flight *connected* sessions to finish; `--force` stops them. Stop is a single binary; the process exits when the drain window closes. | Maintenance-marker file + `MaintenanceHandler` (drains ALL running sessions, not just connected ones, before signaling). | **Stock is stricter** (only connected sessions matter; idle-connected sessions don't block). The fork's "drain all" is wrong for an API used by an always-on LLM client. **Use stock `--drain`.** |
| **Lazy respawn** | Always — the next `omni` invocation spawns a fresh server on the new code automatically. `rebuild/upstream-0.7` has no `current` symlink / systemd drop-in. | The next `omni` invocation re-launches the systemd unit; the unit's drop-in pins the executable to the new release. | **Stock sufficient for a non-immortal-runtime install.** For a pinned-immutable-runtime install, the deployment tooling layers a thin `promote_release.sh` over `omni upgrade` that swaps the drop-in and restarts — see §3. |
| **Migration rehearsal** | `omni upgrade` does not run a migration rehearsal; it trusts the wheel to be consistent with the database head. | `rehearse()` in `omnigent/updater/migration_rehearsal.py` runs `alembic upgrade head` against a copy of the live database inside a tempdir, then compares heads. | **Custom-only.** Stock `omni upgrade` does not do this. The custom rehearse is the **only** mechanism the fork provides for "the candidate release is migration-safe". |
| **Canary startup** | n/a | `canary/_spawn_canary` runs the candidate release in a release-local artifact dir, points the loopback at it, and probes `/health` + `/` + `/_deployed_sha`. | **Custom-only.** The canary is what catches "the candidate release boots but the unit template is wrong". |
| **Health-gated cutover** | n/a | `_default_health_probes(expected_sha)` checks the recorded `deployed-sha`, the live systemd unit, the loopback probe, and the public probe. Fail-closed. | **Custom-only.** Stock `omni upgrade` does not gate on a health check before declaring success. |
| **Rollback to exact previous SHA** | `omni upgrade` cannot roll back; rolling back is "install the previous wheel". | `scripts/rollback_release.sh` swaps `current` symlink, rewrites drop-ins, restarts, runs the same health-gated probe, and rewrites `deployed-sha` *only after* the probe passes. | **Custom-only.** `omni upgrade` is forward-only. |
| **Production promotion gate** | None — `omni upgrade` is fully automated and would auto-promote production. | `promote_release.sh` is the human-gated cutover. | **Custom-only.** Stock has no production-promotion gate; running `omni upgrade` against the live install would immediately swap binaries. |

## 2. Recommendation: **extend upstream updater minimally, then keep production promotion approval-gated**

The custom updater is doing work that stock `omni upgrade` cannot do:
migration rehearsal, canary startup, health-gated cutover, and rollback
to the exact previous SHA. None of those are appropriate as upstream
changes (they are deployment-specific, not stock-runtime concerns), but
all of them are necessary for safe production promotion.

### Decision matrix

| Item | Decision |
| --- | --- |
| **Update detection** | **Use stock `omni upgrade --check`.** No new code. |
| **Drain posture** | **Use stock `--drain`.** The fork's "drain all" over-drains; stock is correct. |
| **Lazy respawn** | **Use stock** for the simple case. The deployment layer adds the immutable-release drop-in pattern when the install needs to pin the binary. |
| **Migration rehearsal** | **Re-implement in `deploy/control-room/updater/rehearse.py`** as a sibling to the existing `scripts/install_omnigent_updater.sh`. Calls `python -m alembic upgrade head` against a tempdir copy of the live DB. |
| **Canary startup** | **Re-implement in `deploy/control-room/updater/canary.py`**. Spawns the candidate release in a release-local artifact dir, runs the loopback probe, tears down. |
| **Health-gated cutover** | **Re-implement in `deploy/control-room/updater/health.py`**. Reads `deployed-sha`, the systemd unit state, the loopback probe, and the public probe. |
| **Rollback to exact previous SHA** | **Re-implement in `deploy/control-room/updater/rollback.sh`**. `current → previous` symlink swap, drop-in rewrite, restart, health probe, `deployed-sha` rewrite. |
| **Production promotion gate** | **Keep approval-gated.** `promote_release.sh` is the human-gated cutover; it is the only entry point that swaps the `current` symlink and restarts the live unit. `omni upgrade` is never invoked on production. |

### Out of scope

- **Unattended production promotion.** Not enabled. Every successful
  production promotion is preceded by a candidate rehearsal on a
  disposable database copy, a canary probe, and a manual
  `scripts/promote_release.sh` invocation. Once a pattern of upgrades
  has succeeded N times without a rollback, this can be revisited.
- **Auto-rollback on a single probe failure.** The fork's
  `promote_release.sh` already does this; the rebuild branch preserves
  the same contract. The auto-rollback window is the canary phase +
  the first 60 s of cutover; after that, an operator decides.
- **Bypassing the `deployed-sha` marker.** The fork's `promote_release.sh`
  always rewrites `deployed-sha` *after* the health probe passes; the
  rebuild branch preserves this ordering.

## 3. Final architecture

```
production
  /var/lib/omnigent/shared/deployed-sha       [canonical live SHA marker]
  /home/hermes/workspace/deployments/omnigent/
    releases/<sha>/                          [immutable, by-SHA]
    releases/current -> <sha>                 [symlink, promoted]
    releases/previous -> <sha>                [symlink, pre-promotion]
  /etc/systemd/system/
    omnigent-eval-web.service                [unit template]
    omnigent-eval-web.service.d/10-release-<sha>.conf   [drop-in]

operator
  gh workflow run release.yml -f version=0.8.0 -f dry_run=false   [upstream]
  scripts/control-room/updater/rehearse.sh <sha>                 [db-safe check]
  scripts/control-room/updater/canary.sh <sha>                   [canary probe]
  scripts/control-room/updater/health.sh <sha>                    [preflight]
  scripts/control-room/updater/promote_release.sh <sha>           [human-gated]
  scripts/control-room/updater/rollback_release.sh               [human-gated]
  scripts/control-room/updater/cleanup_releases.sh               [maintenance]
```

The release pipeline is `omni upgrade` for the *upstream* artifact
(only used in development, never in production); the production
promotion is the eight-script `deploy/control-room/updater/` set,
gated on `deployed-sha` + the canary health probe.
