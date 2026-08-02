#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Check 1 — Omnigent boots + fresh DB initializes to upstream head
# ─────────────────────────────────────────────────────────────────────
#
# Verifies that the rebuild's omnigent wheel boots against an
# empty OMNIGENT_DATA_DIR, the first-boot migration path fires,
# and the resulting schema is at upstream v0.7 head.
#
# Requires: sqlite3 on PATH (the canary runner installs it
# before invoking this script).
#
# Inputs (env vars, set by canary.sh):
#   OMNIGENT_DATA_DIR   the data dir to verify (must be empty
#                       except for the chat.db the wheel creates)
#   OMNIGENT_PORT       the TCP port the canary wheel binds
#   UPSTREAM_HEAD_SHA   the expected alembic_version row, e.g.
#                       zf1a2b3c4d5e
#   UNIT_NAME           the systemd unit name (default: omnigent)
#
# Output: prints "PASS" or "FAIL <reason>" on stdout; returns 0
# on PASS, 1 on FAIL.

set -eu

: "${OMNIGENT_DATA_DIR:=/var/lib/canary-omnigent}"
: "${OMNIGENT_PORT:=6767}"
: "${UPSTREAM_HEAD_SHA:=zf1a2b3c4d5e}"
: "${UNIT_NAME:=omnigent}"

ok()  { printf 'PASS\n'; exit 0; }
bad() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

DB_PATH="$OMNIGENT_DATA_DIR/chat.db"
[ -f "$DB_PATH" ] || bad "chat.db not present at $DB_PATH (the wheel did not boot, or its first-boot migration did not fire)"

# Required tables.
TABLES=$(sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
for required in alembic_version agents conversations comments files policies permissions scheduled_tasks projects inbox tasks; do
  case "$TABLES" in
    *"$required"*) ;;
    *) bad "expected table $required missing from fresh schema; got: $(printf '%s' "$TABLES" | tr '\n' ' ')" ;;
  esac
done

# Alembic head.
HEAD=$(sqlite3 "$DB_PATH" "SELECT version_num FROM alembic_version")
[ "$HEAD" = "$UPSTREAM_HEAD_SHA" ] || bad "alembic_version=$HEAD (expected $UPSTREAM_HEAD_SHA)"

# /health.
HEALTH_URL="http://127.0.0.1:${OMNIGENT_PORT}/health"
HEALTHY=0
for _ in $(seq 1 45); do
  if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
[ "$HEALTHY" = 1 ] || bad "/health did not return 200 within 45 s"

# journalctl first-boot log line (when systemd is in play).
if command -v journalctl >/dev/null 2>&1; then
  JOURNAL=$(journalctl -u "$UNIT_NAME" -n 100 --no-pager 2>/dev/null || true)
  if printf '%s' "$JOURNAL" | grep -q 'Running database migrations'; then
    :
  else
    printf 'note: did not observe "Running database migrations" in journalctl (wheel may have skipped the log line)\n' >&2
  fi
  if printf '%s' "$JOURNAL" | grep -q 'schema is out of date'; then
    bad "journalctl reports 'schema is out of date'"
  fi
fi

ok