# `scripts/cutover.sh` — in-place Omnigent 0.7 cutover

The single command that replaces the running production
`omnigent.service` with the rebuild release of Omnigent 0.7.

## What it does (in order)

1. **Stop** the existing `omnigent.service`.
2. **Backup** the existing `chat.db` to
   `<data_dir>/backups/chat.db.<UTC-timestamp>.bak`. Verification:
   - timestamped backup filename
   - size > 0
   - SHA-256 matches the live file's pre-copy hash
   - `sqlite3 <backup> ".schema"` returns non-empty output
   - `chmod 0444` (read-only)
   - best-effort `chattr +i` (skipped gracefully if the
     filesystem doesn't support it)
3. **Move aside** the old live DB to
   `<db_path>.pre-0.7.<UTC-timestamp>` (preserved for forensic
   inspection).
4. **Install** the rebuild wheel — `uv tool install --force`
   (canonical, per upstream's `scripts/install_oss.sh`), or
   `pipx install --force`, or `pip install --user --force-reinstall`.
5. **Resolve** the installed `omni` shim absolute path
   (`command -v omni && readlink -f $(command -v omni)`).
6. **Render** the systemd unit from
   `deploy/rebuild/systemd/omnigent.service.template`, substituting
   the placeholders (`<OMNI_BIN_PATH>`, `<SERVICE_USER>`,
   `<PROD_PORT>`, `<PROD_HOSTNAME>`).
7. **Install** the rendered unit; `systemctl daemon-reload`.
8. **Start** the service.
9. **Poll** `/health` for up to 45 s.
10. **Confirm** the first-boot migration log line
    (`Running database migrations`) in `journalctl`.
11. **Smoke-test** `/health`, `/api/whoami`, `POST /v1/sessions`.
12. **Stamp** `<data_dir>/deployed-sha` with the rebuild SHA.

## Idempotency

- Re-running on top of the same release: backup is regenerated,
  DB is moved aside again, wheel is reinstalled (uv tool's
  `--force` makes this a no-op or an upgrade), unit is rewritten,
  service is restarted. `deployed-sha` is rewritten with the same
  value.
- Re-running on top of a failed release: backup verification +
  move-aside succeed; install + unit + start retry against the
  same wheel.
- Re-running on top of a stale release: backup rotates to a
  new timestamp; the stale wheel is replaced; unit + service
  restart on the new release.

## Backup immutability is optional, not required

`chattr +i` is attempted as a best-effort step. It is supported
on ext4 / xfs. It is NOT supported on:

- tmpfs (e.g. a Docker layer)
- overlayfs (typical container rootfs)
- Some FUSE filesystems
- Some network filesystems (NFS without `no_root_squash`,
  some SMB configurations)

A filesystem that does not support `chattr` does **NOT** fail the
deployment. The required guarantees — timestamped backup, size
verification, SHA-256 verification, sqlite schema read,
`chmod 0444` — all run unconditionally. `chattr +i` is logged as
a warning when unsupported.

## Required backups (unconditional)

| Check | Command |
| --- | --- |
| Backup file exists at `<data_dir>/backups/chat.db.<UTC-ts>.bak` | `ls -l $BACKUP_PATH` |
| Size > 0 | `stat -c %s $BACKUP_PATH` |
| SHA-256 matches the live file's pre-copy hash | `sha256sum $BACKUP_PATH` (compared against the live SHA captured before the copy) |
| Backup is a real SQLite database | `sqlite3 $BACKUP_PATH ".schema"` |
| Read-only permissions | `ls -l $BACKUP_PATH` shows mode `0444` |

If any of these checks fail, the script `die`s with an explicit
error and exits non-zero. The production service MAY be in a
half-state at this point; read the log file before re-running.

## Required flags

| Flag | Why |
| --- | --- |
| `--confirm` | Acknowledges that this script stops and restarts a running service. Without it, the script prints the planned actions and exits 0. |

## Optional flags

| Flag | Effect |
| --- | --- |
| `--dry-run` | Performs the backup + verify + move-aside steps; does NOT install the wheel, does NOT touch the unit, does NOT start the service. Exit 0 when the backup is sound. |
| `--skip-systemd` | Does not touch the unit file. Used by the Phase D disposable-host canary. |
| `--rebuild-sha <sha>` | Label written to `<data_dir>/deployed-sha`. |
| `--wheel <path>` | Install a specific local wheel instead of the configured index. |
| `--port <port>` | Override the production TCP port (default: read from the existing unit file, fall back to 6767). |
| `--host <ip>` | Bind host (default: `0.0.0.0`). |
| `--user <user>` | Service user (default: read from the existing unit file, fall back to `root`). |
| `--unit <name>` | Systemd unit name (default: `omnigent`). |

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Cutover completed successfully (or dry-run completed with backup verified). |
| 1 | Fatal error; production may be in a half-state. Read the log; inspect with `systemctl status` / `journalctl -u omnigent -n 50`. |
| 2 | Bad arguments. |

## Log file

Every cutover writes a per-run log to
`${LOG_DIR:-/var/log/omnigent-cutover}/cutover.<UTC-timestamp>.log`.
Both stdout (the human-facing summary) and stderr (the
`log INFO/ERROR/WARN` lines) write to it. The summary includes
every resolved path: backup path, moved-aside path, omni binary,
install method, deployed-sha contents.

## Not handled by this script

- **The canary.** This script is the cutover step itself. The
  Phase D canary (against a disposable VM) is a separate workflow
  documented in `deploy/rebuild/tests/README.md`.
- **Rollback to a previous rebuild release.** This script only
  installs the rebuild wheel and writes the unit. If the cutover
  succeeds but the verification smoke tests fail, the operator
  re-runs with `--wheel /srv/omnigent/wheels/omnigent-<previous-rebuild-sha>.whl`
  to roll back. There is no automatic rollback.
- **The Database migrations themselves.** This script does not
  run `alembic upgrade head`. Upstream v0.7's first-boot
  `_initialize_or_verify_schema` runs it automatically when the
  new wheel boots against an empty database file.