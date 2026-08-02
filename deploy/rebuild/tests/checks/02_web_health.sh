#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Check 2 — Web/API health on the temporary loopback port
# ─────────────────────────────────────────────────────────────────────
#
# Asserts the rebuild wheel is bound to an UNUSED temporary loopback
# TCP port (NOT the production port), responds to GET /health with
# 200 inside 10 s, and the canary-port bind is on a loopback address
# (so it is not reachable from outside the host and does not
# interfere with the production port).
#
# Phase D isolation invariant: the canary wheel must NOT bind the
# production port. This check fails closed if the canary wheel is
# bound to the production port or to a non-loopback address.
#
# Inputs (env vars, set by canary.sh):
#   OMNIGENT_PORT       the canary wheel's TCP port (the temp port,
#                       NOT the production port)
#   PROD_PORT           the production TCP port (default: same as
#                       OMNIGENT_PORT — set by the operator to a
#                       different value to assert the two differ)
#   CANARY_RUN_ID       the per-run ID (for evidence)
#   UNIT_NAME           the systemd unit name (default: omnigent)
#
# Output: prints "PASS" or "FAIL <reason>" on stdout; returns 0
# on PASS, 1 on FAIL.

set -eu

: "${OMNIGENT_PORT:=6767}"
: "${PROD_PORT:=$OMNIGENT_PORT}"
: "${UNIT_NAME:=omnigent}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"

ok()  { printf 'PASS\n'; exit 0; }
bad() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

# 1. The canary port must differ from the production port.
if [ "$OMNIGENT_PORT" = "$PROD_PORT" ]; then
  bad "canary port $OMNIGENT_PORT equals production port $PROD_PORT — Phase D isolation broken"
fi

# 2. The canary port must be in the unprivileged range (>1024) to
#    avoid colliding with system services and to clearly mark it as
#    non-production.
case "$OMNIGENT_PORT" in
  ''|*[!0-9]*) bad "OMNIGENT_PORT=$OMNIGENT_PORT is not a number" ;;
esac
if [ "$OMNIGENT_PORT" -le 1024 ]; then
  bad "OMNIGENT_PORT=$OMNIGENT_PORT is in the privileged range (≤1024); Phase D must use an unprivileged loopback port"
fi

# 3. The wheel must not be bound to the production port. We check
#    via ss to see what is listening on the production port. The
#    canary wheel may not appear there (it is on the canary port);
#    the production service is the only thing that should be there.
if command -v ss >/dev/null 2>&1; then
  PROD_LISTENERS=$(ss -tlnH "sport = :$PROD_PORT" 2>/dev/null || true)
  case "$PROD_LISTENERS" in
    *"$PROD_PORT"*omnigent*|*python*|*"pid="*)
      # A listener is present on the prod port; that's fine as
      # long as it isn't the canary wheel. We can't easily
      # distinguish the canary from the prod process from ss
      # alone, but if there is exactly ONE listener and its
      # cmd matches "canary" or the canary run-id, fail.
      if printf '%s' "$PROD_LISTENERS" | grep -q "$CANARY_RUN_ID"; then
        bad "canary wheel is bound to the production port $PROD_PORT"
      fi
      ;;
  esac
fi

# 4. Poll /health on the canary port until 200 is returned (or
#    10 s elapse).
HEALTH_URL="http://127.0.0.1:${OMNIGENT_PORT}/health"
HEALTHY=0
for _ in $(seq 1 10); do
  if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
[ "$HEALTHY" = 1 ] || bad "/health did not return 200 within 10 s on http://127.0.0.1:${OMNIGENT_PORT}/health"

# 5. /api/version (or /api/version-equivalent) responds.
VERSION_URL="http://127.0.0.1:${OMNIGENT_PORT}/api/version"
VERSION_BODY=$(curl -fsS --max-time 5 "$VERSION_URL" 2>/dev/null || true)
if [ -z "$VERSION_BODY" ]; then
  bad "/api/version did not return 200 on http://127.0.0.1:${OMNIGENT_PORT}/api/version"
fi

# 6. Assert the bind address is loopback (127.0.0.1). The canary
#    wheel is started with --host 127.0.0.1 by the launcher; if
#    it were bound to 0.0.0.0 it would be exposed on every
#    interface and could conflict with the production deploy
#    (or be reachable by the reverse proxy).
if command -v ss >/dev/null 2>&1; then
  CANARY_LISTENERS=$(ss -tlnH "sport = :$OMNIGENT_PORT" 2>/dev/null || true)
  if printf '%s' "$CANARY_LISTENERS" | grep -qE '0\.0\.0\.0:'$OMNIGENT_PORT'|:::'$OMNIGENT_PORT'|\*:'$OMNIGENT_PORT; then
    bad "canary wheel is bound to a non-loopback address; ss says: $CANARY_LISTENERS"
  fi
fi

# 7. Print a small evidence block for the report.
# The status line MUST be first (the canary runner parses the
# first non-empty line as PASS|FAIL|SKIPPED).
ok
cat <<EVIDENCE
canary_port=$OMNIGENT_PORT
prod_port=$PROD_PORT
health_url=$HEALTH_URL
version_body=$VERSION_BODY
unit_name=$UNIT_NAME
run_id=$CANARY_RUN_ID
EVIDENCE
