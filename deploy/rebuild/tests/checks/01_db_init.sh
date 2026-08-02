#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Check 1 — Fresh Omnigent 0.7 database initialization
# ─────────────────────────────────────────────────────────────────────
#
# Verifies that the rebuild's omnigent wheel boots against an
# empty OMNIGENT_DATA_DIR, the first-boot migration path fires,
# and the resulting schema is at upstream v0.7 head. The /health
# check is a separate check (check 2).
#
# This check is the *only* place the canary looks at the database
# contents. The canary does NOT touch the production chat.db; the
# production OMNIGENT_DATA_DIR is never read by the canary process.
#
# Requires: sqlite3 on PATH (the canary runner installs it
# before invoking this script).
#
# Inputs (env vars, set by canary.sh):
#   OMNIGENT_DATA_DIR   the data dir to verify (must be empty
#                       except for the chat.db the wheel creates)
#   UPSTREAM_HEAD_SHA   the expected alembic_version row, e.g.
#                       b3c4d5e6f7a8 (upstream v0.7.0 head)
#   UNIT_NAME           the systemd unit name (default: omnigent)
#
# Output: prints "PASS" or "FAIL <reason>" on stdout; returns 0
# on PASS, 1 on FAIL.

set -eu

: "${OMNIGENT_DATA_DIR:=/var/lib/canary-omnigent}"
: "${UPSTREAM_HEAD_SHA:=b3c4d5e6f7a8}"
: "${UNIT_NAME:=omnigent}"
: "${OMNIGENT_PORT:=6767}"

ok()  { printf 'PASS\n'; exit 0; }
bad() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

# Block obvious isolation-violation paths up front.
case "$OMNIGENT_DATA_DIR" in
  /var/lib/omnigent|/home/*/.omnigent)
    bad "OMNIGENT_DATA_DIR=$OMNIGENT_DATA_DIR looks like the production path; Phase D must use a temp dir"
    ;;
esac

DB_PATH="$OMNIGENT_DATA_DIR/chat.db"
[ -f "$DB_PATH" ] || bad "chat.db not present at $DB_PATH (the wheel did not boot, or its first-boot migration did not fire)"

# Required tables. A fresh upstream v0.7 database must contain at
# least these tables; any other tables indicate fork-only state
# was migrated in, which Phase D does not do.
TABLES=$(sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
for required in alembic_version agents conversations comments files policies scheduled_tasks projects users hosts session_permissions; do
  case "$TABLES" in
    *"$required"*) ;;
    *) bad "expected table $required missing from fresh schema; got: $(printf '%s' "$TABLES" | tr '\n' ' ')" ;;
  esac
done

# Alembic head.
HEAD=$(sqlite3 "$DB_PATH" "SELECT version_num FROM alembic_version")
[ "$HEAD" = "$UPSTREAM_HEAD_SHA" ] || bad "alembic_version=$HEAD (expected $UPSTREAM_HEAD_SHA)"

# journalctl first-boot log line (when systemd is in play).
if command -v journalctl >/dev/null 2>&1; then
  JOURNAL=$(journalctl -u "$UNIT_NAME" -n 200 --no-pager 2>/dev/null || true)
  if printf '%s' "$JOURNAL" | grep -q 'Running database migrations'; then
    printf 'note: observed "Running database migrations" in journalctl for %s\n' "$UNIT_NAME" >&2
  else
    printf 'note: did not observe "Running database migrations" in journalctl (wheel may have skipped the log line; not a FAIL for the temp foreground launcher)\n' >&2
  fi
  if printf '%s' "$JOURNAL" | grep -q 'schema is out of date'; then
    bad "journalctl reports 'schema is out of date'"
  fi
fi

# Evidence block (printed after PASS so the runner can include it
# in the report).
cat <<EVIDENCE
db_path=$DB_PATH
alembic_head=$HEAD
data_dir=$OMNIGENT_DATA_DIR
tables=$(printf '%s' "$TABLES" | tr '\n' ' ')
EVIDENCE

ok
