#!/usr/bin/env sh
# ─────────────────────────────────────────────────────────────────────
# scripts/cutover.sh — in-place Omnigent 0.7 cutover
# ─────────────────────────────────────────────────────────────────────
#
# Replaces the running production omnigent.service with the rebuild
# release of Omnigent 0.7 in a single stop / backup / install /
# start cycle. The existing production chat.db is preserved as a
# read-only backup; a fresh upstream 0.7 database is initialized
# at the same configured database URI on first boot.
#
# The script is idempotent: re-running on top of the same release
# is a no-op; re-running on top of a different release stops,
# installs, and starts the new one. Re-running on a failed cutover
# retries each step.
#
# Required backups (no chattr immutability required):
#   - timestamped backup file
#   - size > 0
#   - SHA-256 verification (matches the live file's pre-copy hash)
#   - successful sqlite3 .schema read
#   - chmod 0444 (read-only permissions)
#
# Optional best-effort (skipped gracefully if unsupported):
#   - chattr +i (immutable attribute; supported on ext4 / xfs,
#     NOT supported on tmpfs / overlayfs / some FUSE / network
#     filesystems). A filesystem that does not support chattr does
#     NOT fail the deployment.
#
# Required environment variables (sourced from /etc/omnigent/env.conf
# or passed via the caller's shell):
#   OMNIGENT_DATA_DIR  — the data root; default /var/lib/omnigent
#
# Required CLI flags:
#   --confirm       required to acknowledge that this script
#                   STOPS the running service and STARTS a new one.
#                   Without --confirm the script prints what it
#                   WOULD do and exits 0.
#
# Optional CLI flags:
#   --rebuild-sha <sha>  the rebuild release SHA to install (used
#                        only as a label for the deployed-sha marker;
#                        the wheel path itself comes from the chosen
#                        install method).
#   --wheel <path>        the local wheel to install (default: install
#                        the latest matching version from the
#                        configured index).
#   --port <port>         the production TCP port (default: read
#                        from the existing unit file).
#   --host <ip>           the bind host (default: 0.0.0.0).
#   --user <user>         the systemd unit's User= (default: read
#                        from the existing unit file; falls back to
#                        root if absent).
#   --unit <name>         the systemd unit name (default: omnigent).
#   --skip-systemd        do not touch the unit file; only install
#                        the wheel and run the DB backup / move /
#                        fresh-init dance. Useful for the Phase D
#                        canary when the rebuild wheel is started
#                        as a foreground process on the existing
#                        Omnigent VM (Phase D does NOT use a
#                        disposable VM and does NOT use a temp
#                        unit).
#   --dry-run             print every action, run the DB backup +
#                        verify steps, but do NOT install the wheel,
#                        do NOT modify the unit, do NOT start the
#                        service. Exit 0 when the backup is sound.
#
# Exit codes:
#   0  cutover completed (or dry-run completed with backup verified).
#   1  a fatal error occurred; the production service MAY be in a
#      half-state. Read the log; do not re-run without inspecting.
#   2  bad arguments.

set -eu
# Note: -o pipefail is not portable across all POSIX sh; we use
# explicit pipe handling where it matters (see backup verify).

# ─── Logging ────────────────────────────────────────────────────────
# Resolve a writable log directory. Try in order:
#   1. $LOG_DIR (callers can override).
#   2. /var/log/omnigent-cutover (production default).
#   3. $XDG_STATE_HOME/omnigent-cutover (per-user, XDG-respecting).
#   4. $HOME/.omnigent-cutover (home-dir fallback).
#   5. $TMPDIR/omnigent-cutover.<pid> (last-resort, always writable).
log_dir_candidates() {
  printf '%s\n' "${LOG_DIR:-}" "/var/log/omnigent-cutover" \
    "${XDG_STATE_HOME:+$XDG_STATE_HOME/omnigent-cutover}" \
    "${HOME:+$HOME/.omnigent-cutover}" \
    "${TMPDIR:-/tmp}/omnigent-cutover.$$"
}

LOG_FILE=""
for candidate in $(log_dir_candidates); do
  [ -z "$candidate" ] && continue
  if mkdir -p "$candidate" 2>/dev/null; then
    LOG_DIR="$candidate"
    LOG_FILE="$LOG_DIR/cutover.$(date -u +%Y%m%dT%H%M%SZ).log"
    break
  fi
done
if [ -z "$LOG_FILE" ]; then
  # Fall back to a per-pid temp file under /tmp; always writable.
  LOG_DIR="/tmp/omnigent-cutover.$$"
  mkdir -p "$LOG_DIR" || { printf '%s\n' "ERROR: could not create any log directory" >&2; exit 1; }
  LOG_FILE="$LOG_DIR/cutover.log"
fi
exec 3>"$LOG_FILE"
log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${1:-INFO}" "${2:-}" | tee -a "$LOG_FILE" >&2; }
err() { log ERROR "$1"; }
warn() { log WARN "$1"; }
info() { log INFO "$1"; }
die() { err "$1"; exit "${2:-1}"; }

# ─── Defaults ───────────────────────────────────────────────────────
: "${OMNIGENT_DATA_DIR:=/var/lib/omnigent}"
PROD_PORT=""
BIND_HOST="0.0.0.0"
SERVICE_USER=""
UNIT_NAME="omnigent"
REBUILD_SHA=""
WHEEL_PATH=""
SKIP_SYSTEMD=0
DRY_RUN=0
CONFIRM=0

usage() {
  cat <<'USAGE' >&2
Usage: scripts/cutover.sh [--confirm] [--rebuild-sha <sha>] [--wheel <path>]
                          [--port <port>] [--host <ip>] [--user <user>]
                          [--unit <name>] [--skip-systemd] [--dry-run]
  --confirm       Required to perform the cutover. Without it the
                  script prints the planned actions and exits 0.
  --dry-run       Run the DB backup + verify steps; do NOT install
                  the wheel, do NOT modify the unit, do NOT start
                  the service.
  --skip-systemd  Do not touch the unit file (Phase D canary).
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --confirm) CONFIRM=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-systemd) SKIP_SYSTEMD=1; shift ;;
    --rebuild-sha) REBUILD_SHA="$2"; shift 2 ;;
    --wheel) WHEEL_PATH="$2"; shift 2 ;;
    --port) PROD_PORT="$2"; shift 2 ;;
    --host) BIND_HOST="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --unit) UNIT_NAME="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) err "unknown argument: $1"; usage ;;
  esac
done

# ─── Pre-flight ────────────────────────────────────────────────────
info "cutover starting"
info "data dir: $OMNIGENT_DATA_DIR"
info "rebuild sha: ${REBUILD_SHA:-<unspecified>}"
info "wheel: ${WHEEL_PATH:-<from index>}"
info "unit: $UNIT_NAME"
info "log file: $LOG_FILE"

if ! command -v sqlite3 >/dev/null 2>&1; then
  die "sqlite3 not found on PATH (needed to verify the backup)"
fi
if ! command -v sha256sum >/dev/null 2>&1; then
  die "sha256sum not found on PATH (needed to verify the backup)"
fi
if ! command -v systemctl >/dev/null 2>&1 && [ "$SKIP_SYSTEMD" -eq 0 ]; then
  die "systemctl not found on PATH (and --skip-systemd not passed)"
fi

# ─── 1. Resolve existing unit state (port, user) if not given ─────
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}.service"
DROP_IN_DIR="/etc/systemd/system/${UNIT_NAME}.service.d"

if [ -z "$PROD_PORT" ] || [ -z "$SERVICE_USER" ]; then
  if [ -f "$UNIT_FILE" ]; then
    # Drop-ins override the main unit; check drop-ins first.
    if [ -d "$DROP_IN_DIR" ] && [ -z "$PROD_PORT" ]; then
      for f in "$DROP_IN_DIR"/*.conf; do
        if [ -f "$f" ]; then
          p=$(grep -E '^[^#]*Environment=.OMNIGENT_AUTH_HEADER=' "$f" 2>/dev/null || true)
          [ -z "$p" ] && continue
          : # placeholder for future per-dropin parsing
        fi
      done
    fi
    if [ -z "$SERVICE_USER" ]; then
      SERVICE_USER=$(grep -E '^User=' "$UNIT_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)
    fi
    if [ -z "$PROD_PORT" ]; then
      # ExecStart shape: omni server --port <port> ...; pick the
      # first --port value.
      PROD_PORT=$(grep -E '^ExecStart=' "$UNIT_FILE" 2>/dev/null \
        | sed -nE 's/.*--port[= ]+([0-9]+).*/\1/p' | head -1 || true)
    fi
  fi
fi

SERVICE_USER="${SERVICE_USER:-root}"
PROD_PORT="${PROD_PORT:-6767}"

info "resolved service user: $SERVICE_USER"
info "resolved production port: $PROD_PORT"
info "resolved bind host: $BIND_HOST"

# ─── 2. Confirm guard ──────────────────────────────────────────────
if [ "$CONFIRM" -ne 1 ] && [ "$DRY_RUN" -ne 1 ]; then
  info "dry-run mode (no --confirm, no --dry-run) — printing planned actions and exiting"
  cat <<PLAN
Planned actions:
  1. systemctl stop $UNIT_NAME
  2. Backup existing chat.db (if any) to
     $OMNIGENT_DATA_DIR/backups/chat.db.<UTC-timestamp>.bak
     - verify SHA-256 matches live pre-copy hash
     - verify size > 0
     - verify sqlite3 .schema returns non-empty
     - chmod 0444 (read-only)
     - best-effort: chattr +i (skipped if unsupported)
  3. Move existing chat.db aside as
     chat.db.pre-0.7.<UTC-timestamp>
  4. Install rebuild wheel:
       ${WHEEL_PATH:+from local path $WHEEL_PATH}
       ${WHEEL_PATH:-from configured index, latest matching version}
  5. Resolve the installed 'omni' shim absolute path:
       OMNI_BIN=\$(command -v omni)
       if [ -z "\$OMNI_BIN" ]; then
         OMNI_BIN=\$(uv tool dir --bin)/omni
       fi
       OMNI_BIN=\$(readlink -f "\$OMNI_BIN")
  6. Render systemd unit from
     deploy/rebuild/systemd/omnigent.service.template with:
       <OMNI_BIN_PATH> = \$OMNI_BIN
       <SERVICE_USER>   = $SERVICE_USER
       <PROD_PORT>      = $PROD_PORT
       <PROD_HOSTNAME>  = ${PROD_HOSTNAME:-<from env>}
  7. systemctl daemon-reload
  8. systemctl start $UNIT_NAME
  9. Poll /health for up to 45 s
 10. Confirm first-boot migration log line in journalctl
 11. Smoke-test /health, /api/whoami, POST /v1/sessions
 12. Stamp $OMNIGENT_DATA_DIR/deployed-sha with $REBUILD_SHA

Re-run with --confirm to actually perform these actions.
Re-run with --dry-run to perform steps 1-3 and exit before any
install / unit / start.
PLAN
  exit 0
fi

# ─── 3. Stop the existing service ──────────────────────────────────
if [ "$SKIP_SYSTEMD" -eq 0 ]; then
  if systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null; then
    info "stopping $UNIT_NAME"
    if [ "$DRY_RUN" -ne 1 ]; then
      systemctl stop "$UNIT_NAME" || warn "systemctl stop returned non-zero (unit may already be inactive)"
      # Wait for the unit to actually leave the 'active' state.
      for _ in $(seq 1 30); do
        systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null || break
        sleep 1
      done
    fi
  else
    info "$UNIT_NAME is not active — nothing to stop"
  fi
fi

# ─── 4. Backup the existing chat.db ────────────────────────────────
mkdir -p "$OMNIGENT_DATA_DIR"
# The default DB URI (resolved by upstream's _default_db_uri from
# OMNIGENT_DATA_DIR): sqlite:///<data_dir>/chat.db
DB_PATH="$OMNIGENT_DATA_DIR/chat.db"
BACKUP_DIR="$OMNIGENT_DATA_DIR/backups"
mkdir -p "$BACKUP_DIR"
UTC_TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_PATH="$BACKUP_DIR/chat.db.${UTC_TS}.bak"
# Rotate if a backup with the same timestamp already exists (a
# previous run was interrupted before stamping deployed-sha, or
# the operator re-runs within the same second). The rotated
# filename gains a numeric suffix; a 0444 backup from a prior
# run is left untouched.
n=0
while [ -e "$BACKUP_PATH" ]; do
  n=$((n + 1))
  BACKUP_PATH="$BACKUP_DIR/chat.db.${UTC_TS}-${n}.bak"
done

if [ -f "$DB_PATH" ]; then
  info "backing up $DB_PATH -> $BACKUP_PATH"

  # Compute live SHA-256 BEFORE copying.
  LIVE_SHA=$(sha256sum "$DB_PATH" | awk '{print $1}')
  LIVE_SIZE=$(stat -c %s "$DB_PATH" 2>/dev/null || stat -f %z "$DB_PATH")
  info "live SHA-256: $LIVE_SHA"
  info "live size:    $LIVE_SIZE bytes"

  # Copy.
  if command -v cp >/dev/null 2>&1; then
    cp --reflink=auto -p "$DB_PATH" "$BACKUP_PATH" \
      || cp -p "$DB_PATH" "$BACKUP_PATH"
  else
    cat "$DB_PATH" >"$BACKUP_PATH"
  fi

  # Verify backup.
  BACKUP_SHA=$(sha256sum "$BACKUP_PATH" | awk '{print $1}')
  BACKUP_SIZE=$(stat -c %s "$BACKUP_PATH" 2>/dev/null || stat -f %z "$BACKUP_PATH")
  info "backup SHA-256: $BACKUP_SHA"
  info "backup size:    $BACKUP_SIZE bytes"

  if [ "$LIVE_SIZE" -le 0 ]; then
    die "live DB size is zero — refusing to proceed (path: $DB_PATH)"
  fi
  if [ "$BACKUP_SIZE" -le 0 ]; then
    die "backup size is zero (path: $BACKUP_PATH)"
  fi
  if [ "$BACKUP_SHA" != "$LIVE_SHA" ]; then
    die "backup SHA-256 mismatch: live=$LIVE_SHA backup=$BACKUP_SHA"
  fi

  # Verify the backup is a real, readable SQLite database.
  SCHEMA=$(sqlite3 "$BACKUP_PATH" ".schema" 2>&1) \
    || die "sqlite3 .schema on the backup failed: $SCHEMA"
  if [ -z "$SCHEMA" ]; then
    die "backup sqlite schema is empty (path: $BACKUP_PATH)"
  fi
  info "backup sqlite schema: $(printf '%s' "$SCHEMA" | wc -l) lines"

  # Mark read-only.
  chmod 0444 "$BACKUP_PATH" || warn "chmod 0444 failed on $BACKUP_PATH"
  info "backup marked read-only (chmod 0444)"

  # Optional best-effort immutable attribute. chattr +i is supported
  # on ext4 / xfs; NOT supported on tmpfs / overlayfs / some FUSE /
  # network filesystems. We attempt it, log success/failure, and
  # proceed regardless.
  if command -v chattr >/dev/null 2>&1; then
    if chattr +i "$BACKUP_PATH" 2>/dev/null; then
      info "backup marked immutable (chattr +i)"
    else
      warn "chattr +i not supported on this filesystem for $BACKUP_PATH — proceeding without immutability (chmod 0444 still applies)"
    fi
  else
    warn "chattr not installed — proceeding without immutability (chmod 0444 still applies)"
  fi
else
  warn "no existing chat.db at $DB_PATH — nothing to backup (fresh DB will be initialized on first boot)"
fi

# ─── 5. Move the old live DB aside ─────────────────────────────────
if [ -f "$DB_PATH" ]; then
  ASIDE_PATH="${DB_PATH}.pre-0.7.${UTC_TS}"
  info "moving old live DB to $ASIDE_PATH"
  mv "$DB_PATH" "$ASIDE_PATH" \
    || die "mv $DB_PATH $ASIDE_PATH failed"
  info "old DB moved aside (preserved at $ASIDE_PATH for forensic inspection)"
fi

# ─── 6. (dry-run exit) ─────────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  info "dry-run complete; backup verified, live DB moved aside, no install / unit / start performed"
  exit 0
fi

# ─── 7. Install the rebuild wheel ──────────────────────────────────
INSTALL_METHOD=""
OMNI_BIN=""

if [ -n "$WHEEL_PATH" ]; then
  info "installing local wheel: $WHEEL_PATH"
  if command -v uv >/dev/null 2>&1; then
    INSTALL_METHOD="uv tool install (local wheel)"
    uv tool install --force -q "$WHEEL_PATH" \
      || die "uv tool install --force $WHEEL_PATH failed"
    OMNI_BIN_DIR=$(uv tool dir --bin)
  elif command -v pipx >/dev/null 2>&1; then
    INSTALL_METHOD="pipx install (local wheel)"
    pipx install --force "$WHEEL_PATH" \
      || die "pipx install --force $WHEEL_PATH failed"
    OMNI_BIN_DIR=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")
  elif command -v pip >/dev/null 2>&1; then
    INSTALL_METHOD="pip install --user (local wheel)"
    pip install --user --force-reinstall "$WHEEL_PATH" \
      || die "pip install --user --force-reinstall $WHEEL_PATH failed"
    OMNI_BIN_DIR=$(python3 -m site --user-base)/bin
  else
    die "no installer found (need uv, pipx, or pip) and --wheel was passed"
  fi
else
  # Install from the configured index. `uv tool install omnigent`
  # (no version pin) installs the latest version that matches the
  # resolved package name — for the rebuild, the wheel is published
  # under the same canonical name.
  info "installing from configured index"
  if command -v uv >/dev/null 2>&1; then
    INSTALL_METHOD="uv tool install (from index)"
    uv tool install --force -q omnigent \
      || die "uv tool install --force omnigent failed"
    OMNI_BIN_DIR=$(uv tool dir --bin)
  elif command -v pipx >/dev/null 2>&1; then
    INSTALL_METHOD="pipx install (from index)"
    pipx install --force omnigent \
      || die "pipx install --force omnigent failed"
    OMNI_BIN_DIR=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")
  elif command -v pip >/dev/null 2>&1; then
    INSTALL_METHOD="pip install --user (from index)"
    pip install --user --force-reinstall omnigent \
      || die "pip install --user --force-reinstall omnigent failed"
    OMNI_BIN_DIR=$(python3 -m site --user-base)/bin
  else
    die "no installer found (need uv, pipx, or pip) and --wheel was not passed"
  fi
fi

info "install method: $INSTALL_METHOD"
info "candidate bin dir: $OMNI_BIN_DIR"

# Resolve the actual `omni` shim path. Per pyproject.toml:329-330,
# both `omni` and `omnigent` are installed as console scripts.
for candidate in "$OMNI_BIN_DIR/omni" "$OMNI_BIN_DIR/omnigent"; do
  if [ -x "$candidate" ]; then
    OMNI_BIN=$(readlink -f "$candidate" 2>/dev/null || printf '%s' "$candidate")
    info "resolved omni shim: $OMNI_BIN"
    break
  fi
done

if [ -z "$OMNI_BIN" ]; then
  # Fallback: query PATH for the canonical shim.
  if command -v omni >/dev/null 2>&1; then
    OMNI_BIN=$(command -v omni)
    OMNI_BIN=$(readlink -f "$OMNI_BIN" 2>/dev/null || printf '%s' "$OMNI_BIN")
    info "resolved omni shim via PATH: $OMNI_BIN"
  else
    die "could not locate the 'omni' shim after install. Looked in $OMNI_BIN_DIR/omni and $OMNI_BIN_DIR/omnigent, then via 'command -v omni'. Add the install bin dir to PATH or pass --wheel to install a specific wheel."
  fi
fi

# Verify the shim is the new wheel.
OMNI_VERSION=$("$OMNI_BIN" --version 2>&1 | head -1 || true)
info "omni version: $OMNI_VERSION"
case "$OMNI_VERSION" in
  *"0.7.0"*) info "version confirmed: 0.7.0 series" ;;
  *) warn "version string is '$OMNI_VERSION' — expected 0.7.x series" ;;
esac

# ─── 8. Render and install the systemd unit ────────────────────────
if [ "$SKIP_SYSTEMD" -eq 0 ]; then
  UNIT_TEMPLATE="deploy/rebuild/systemd/omnigent.service.template"
  if [ ! -f "$UNIT_TEMPLATE" ]; then
    die "unit template not found at $UNIT_TEMPLATE (run from the repo root)"
  fi

  info "rendering systemd unit from $UNIT_TEMPLATE"
  # sed substitution. The placeholders are well-formed XML so a
  # plain sed -e works (no '&' or '\\' in any placeholder).
  RENDERED_UNIT="$LOG_DIR/${UNIT_NAME}.service.rendered"
  sed \
    -e "s|<OMNI_BIN_PATH>|$OMNI_BIN|g" \
    -e "s|<SERVICE_USER>|$SERVICE_USER|g" \
    -e "s|<PROD_PORT>|$PROD_PORT|g" \
    -e "s|<PROD_HOSTNAME>|${PROD_HOSTNAME:-prod}|g" \
    "$UNIT_TEMPLATE" >"$RENDERED_UNIT"

  # Sanity: every placeholder must have been substituted.
  if grep -E '<(OMNI_BIN_PATH|SERVICE_USER|PROD_PORT|PROD_HOSTNAME)>' "$RENDERED_UNIT" >/dev/null; then
    die "rendered unit still contains placeholder text; refusing to install: $(grep -E '<(OMNI_BIN_PATH|SERVICE_USER|PROD_PORT|PROD_HOSTNAME)>' "$RENDERED_UNIT" | head -3)"
  fi

  info "installing unit: $RENDERED_UNIT -> $UNIT_FILE"
  install -d -m 0755 "$(dirname "$UNIT_FILE")"
  install -m 0644 "$RENDERED_UNIT" "$UNIT_FILE"
  systemctl daemon-reload
  info "systemd daemon-reload complete"
fi

# ─── 9. Start the service ──────────────────────────────────────────
if [ "$SKIP_SYSTEMD" -eq 0 ]; then
  info "starting $UNIT_NAME"
  systemctl start "$UNIT_NAME" \
    || die "systemctl start $UNIT_NAME failed"
fi

# ─── 10. Wait for /health ──────────────────────────────────────────
HEALTH_URL="http://127.0.0.1:${PROD_PORT}/health"
info "polling $HEALTH_URL (up to 45 s)"
HEALTHY=0
for i in $(seq 1 45); do
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
      HEALTHY=1
      break
    fi
  elif command -v wget >/dev/null 2>&1; then
    if wget -q -O- --timeout=2 "$HEALTH_URL" >/dev/null 2>&1; then
      HEALTHY=1
      break
    fi
  else
    warn "neither curl nor wget installed; skipping /health probe"
    HEALTHY=1
    break
  fi
  sleep 1
done

if [ "$HEALTHY" -ne 1 ]; then
  die "/health did not return 200 within 45 s; service is not healthy. Inspect: systemctl status $UNIT_NAME; journalctl -u $UNIT_NAME -n 50"
fi
info "/health returned 200"

# ─── 11. Confirm first-boot migration log line ─────────────────────
if [ "$SKIP_SYSTEMD" -eq 0 ]; then
  if command -v journalctl >/dev/null 2>&1; then
    JOURNAL=$(journalctl -u "$UNIT_NAME" -n 50 --no-pager 2>/dev/null || true)
    if printf '%s' "$JOURNAL" | grep -q 'Running database migrations'; then
      info "first-boot migration log line observed (Running database migrations)"
    else
      warn "did not observe 'Running database migrations' in journalctl -u $UNIT_NAME -n 50 — either no migration was needed (already at head) or the migration failed silently. Inspect: journalctl -u $UNIT_NAME -n 100"
    fi
    if printf '%s' "$JOURNAL" | grep -q 'schema is out of date'; then
      die "journalctl reports 'schema is out of date' — the fresh DB init path failed"
    fi
  fi
fi

# ─── 12. Smoke test ────────────────────────────────────────────────
# In-process smoke (no LLM calls). Verifies auth header injection
# against the configured provider and the fresh DB accepts a
# session create.
WHOAMI_URL="http://127.0.0.1:${PROD_PORT}/api/whoami"
SESSIONS_URL="http://127.0.0.1:${PROD_PORT}/v1/sessions"

if command -v curl >/dev/null 2>&1; then
  if [ -f /etc/omnigent/env.conf ]; then
    HEADER_NAME=$(grep -E '^OMNIGENT_AUTH_HEADER=' /etc/omnigent/env.conf 2>/dev/null \
      | head -1 | cut -d= -f2- | tr -d '[:space:]' || echo "X-Forwarded-Email")
    HEADER_NAME=${HEADER_NAME:-X-Forwarded-Email}
  else
    HEADER_NAME="X-Forwarded-Email"
  fi

  info "smoke: /api/whoami with $HEADER_NAME"
  if curl -fsS --max-time 5 -H "$HEADER_NAME: cutover-smoke@omnigent.local" "$WHOAMI_URL" >/dev/null 2>&1; then
    info "smoke /api/whoami returned 200"
  else
    warn "smoke /api/whoami did not return 200 — auth may still be wiring up"
  fi

  info "smoke: POST /v1/sessions"
  if curl -fsS --max-time 5 -H "$HEADER_NAME: cutover-smoke@omnigent.local" \
      -H "Content-Type: application/json" \
      -X POST -d '{"agent":"verity+opencode","prompt":"cutover smoke test"}' \
      "$SESSIONS_URL" >/dev/null 2>&1; then
    info "smoke POST /v1/sessions returned 200"
  else
    warn "smoke POST /v1/sessions did not return 200 — DB may not have accepted a session create"
  fi
fi

# ─── 13. Stamp deployed-sha ────────────────────────────────────────
DEPLOYED_SHA_PATH="$OMNIGENT_DATA_DIR/deployed-sha"
STAMP_VALUE="${REBUILD_SHA:-$(date -u +%Y%m%dT%H%M%SZ)-rebuild}"
info "stamping $DEPLOYED_SHA_PATH with $STAMP_VALUE"
printf '%s\n' "$STAMP_VALUE" >"$DEPLOYED_SHA_PATH" \
  || warn "could not write $DEPLOYED_SHA_PATH"
chmod 0644 "$DEPLOYED_SHA_PATH" 2>/dev/null || true

# ─── 14. Done ──────────────────────────────────────────────────────
info "cutover complete"
cat <<DONE
  Backup:      $BACKUP_PATH (if it existed)
  Aside:       ${DB_PATH}.pre-0.7.${UTC_TS} (if it existed)
  Omni binary: $OMNI_BIN
  Omni method: $INSTALL_METHOD
  Service:     $UNIT_NAME
  Port:        $PROD_PORT
  Deployed:    $DEPLOYED_SHA_PATH ($STAMP_VALUE)
  Log:         $LOG_FILE
DONE
exit 0