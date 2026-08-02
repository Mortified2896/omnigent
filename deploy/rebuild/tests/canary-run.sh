#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# canary-run.sh — Phase D canary orchestrator for the existing VM
# ─────────────────────────────────────────────────────────────────────
#
# Boots the rebuild's 0.7 wheel against a fresh empty temp data
# dir, waits for it to come up, starts a temporary host runner
# bound to that wheel, verifies the host registered, runs the
# 12-check canary, captures the per-run report, and cleans up
# the temporary host + wheel.
#
# Production is NOT touched:
#   - The canary wheel binds 127.0.0.1:17670 (an unused temp port).
#   - The canary data dir is /tmp/canary-data (NOT the production
#     /home/<user>/.omnigent/chat.db or /var/lib/omnigent/chat.db).
#   - The canary env reads the local OmniRoute at 20128 (NOT the
#     production server at 4097).
#   - The canary does NOT install over the production wheel.
#   - The canary does NOT modify the production systemd unit.
#   - The canary does NOT register a host with production.
#   - The canary's host identity lives under $CANARY_HOME, not
#     under the operator's real ~/.omnigent/.
#
# This is the recommended Phase D entrypoint: it orchestrates the
# wheel + the host + the checks so the canary is reproducible on
# any VM with the build artifacts present.
#
# Requirements:
#   - /tmp/canary-venv/bin/omni (rebuild wheel installed in a
#     temp venv; see the wheel build step in
#     scripts/build_rebuild_wheel.sh or rebuild-with.sh)
#   - python 3.12+, sqlite3, jq, curl, git
#   - Local OmniRoute reachable at OMNIROUTE_BASE_URL (default:
#     http://127.0.0.1:20128/v1)
#   - The rebuild repo at REPO_ROOT (default:
#     /home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7)
#   - The real installed Pi / OpenCode binaries on $PATH
#     (`/home/hermes/.local/bin/pi`, `/home/hermes/.hermes/node/bin/opencode`,
#     or wherever the operator installed them).
#
# Usage:
#   ./canary-run.sh
#   RUN_ID=D-20260101T120000Z ./canary-run.sh
#   ./canary-run.sh --reuse-data-dir   # keep existing canary DB
#   ./canary-run.sh --no-host          # skip host launch (manual runner)
#
# Exit codes:
#   0  all 12 checks PASS (Phase E authorized)
#   1  at least one check FAIL or required SKIPPED
#   2  infrastructure failure (wheel or host did not come up)

set -eu

REBUILD_SHA="${REBUILD_SHA:-$(git -C "${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7}" rev-parse HEAD 2>/dev/null || echo unknown)}"
CANARY_DATA_DIR="${CANARY_DATA_DIR:-/tmp/canary-data}"
CANARY_HOME="${CANARY_HOME:-/tmp/canary-home}"
CANARY_PORT="${CANARY_PORT:-17670}"
PROD_PORT="${PROD_PORT:-4097}"
PROD_HOSTNAME="${PROD_HOSTNAME:-hermes-agent.taile0361b.ts.net}"
RUN_ID="${RUN_ID:-D-canary-$(date -u +%Y%m%dT%H%M%SZ)}"
OMNIROUTE_API_KEY="${OMNIROUTE_API_KEY:-}"
WHEEL_BIN="${WHEEL_BIN:-/tmp/canary-venv/bin/omni}"
REPO_ROOT="${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7}"

REUSE_DATA_DIR=0
LAUNCH_HOST=1
for arg in "$@"; do
  case "$arg" in
    --reuse-data-dir) REUSE_DATA_DIR=1 ;;
    --no-host)        LAUNCH_HOST=0 ;;
    *) ;;
  esac
done

# Where we keep transient logs and pid files for the orchestrator
# itself (separate from the wheel's own logs under $CANARY_DATA_DIR/logs).
ORCH_LOG_DIR="${ORCH_LOG_DIR:-/tmp/canary-orch-logs}"
mkdir -p "$ORCH_LOG_DIR"
WHEEL_LOG="$ORCH_LOG_DIR/wheel.log"
HOST_LOG="$ORCH_LOG_DIR/host.log"

echo "canary-run: starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Stop any prior canary wheel + host (anchored patterns, never
# match the calling bash — see the AGENTS.md pgrep note).
pkill -9 -f '/canary-venv/bin/omni server' 2>/dev/null || true
pkill -9 -f '/canary-venv/bin/omni host'  2>/dev/null || true
sleep 2

# Fresh data dir unless the operator passed --reuse-data-dir.
if [ "$REUSE_DATA_DIR" = 0 ]; then
  echo "canary-run: wiping temp data dir $CANARY_DATA_DIR + canary home $CANARY_HOME"
  rm -rf "$CANARY_DATA_DIR" "$CANARY_HOME"
fi
mkdir -p "$CANARY_DATA_DIR/artifacts" \
         "$CANARY_DATA_DIR/logs" \
         "$CANARY_DATA_DIR/harness-tmp" \
         "$CANARY_DATA_DIR/worktrees"
mkdir -p "$CANARY_HOME/.omnigent"

# Lay out the rebuild's agent bundles in the upstream-canonical
# layout (verity's sub-agents go under verity/agents/).
mkdir -p "$CANARY_DATA_DIR/agents/verity/agents/pi" \
         "$CANARY_DATA_DIR/agents/verity/agents/opencode"
mkdir -p "$CANARY_DATA_DIR/agents/pi" "$CANARY_DATA_DIR/agents/opencode"

cp "$REPO_ROOT/deploy/rebuild/agents/verity/config.yaml" "$CANARY_DATA_DIR/agents/verity/"
cp "$REPO_ROOT/deploy/rebuild/agents/pi/config.yaml"      "$CANARY_DATA_DIR/agents/verity/agents/pi/"
cp "$REPO_ROOT/deploy/rebuild/agents/opencode/config.yaml" \
   "$CANARY_DATA_DIR/agents/verity/agents/opencode/"

# Author the server config.
#
# The canary wheel runs with NO auth provider enabled — this keeps
# the host tunnel's WS handshake accepted under RESERVED_USER_LOCAL
# (see omnigent/server/routes/host_tunnel.py: when ``auth_provider
# is None``, the tunnel accepts with owner=RESERVED_USER_LOCAL).
# That way the temporary host's WS handshake (which carries Origin
# + a Bearer JWT, NOT X-Forwarded-Email) does not get rejected with
# 4004 "unauthenticated".
cat > "$CANARY_DATA_DIR/config.yaml" <<YAML
# Phase D canary server config — temp; isolated from production.
admins:
  - canary@omnigent.local

artifact_location: $CANARY_DATA_DIR/artifacts
database_uri: sqlite:///$CANARY_DATA_DIR/chat.db

copy_max_files: 20
copy_max_total_bytes: 268435456
execution_timeout: 7200

default_agent: verity

routing:
  provider: external
  base_url: ${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128/v1}
  router_name: omniroute
  api_key: \${OMNIROUTE_API_KEY}
YAML

# Author the user config (providers + per-harness routing).
# The host process reads this on connect to resolve provider
# credentials for Pi / OpenCode harness subprocesses.
cat > "$CANARY_HOME/.omnigent/config.yaml" <<YAML
providers:
  omniroute:
    base_url: ${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128/v1}
    api_key_env: OMNIROUTE_API_KEY
    request_timeout_s: 60

harness:
  default:
    model: omniroute/auto
  pi:
    model: omniroute/auto
  opencode:
    model: omniroute/auto

default_agent: verity
YAML

# Path the host subprocess will see for its pi/opencode binaries.
# This matches the operator's normal $PATH so the host subprocess
# can spawn the real installed harnesses.
export PATH="/home/hermes/.local/bin:/home/hermes/.hermes/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Shared env for the wheel and the host. We deliberately do NOT
# set OMNIGENT_AUTH_PROVIDER or OMNIGENT_AUTH_ENABLED — the canary
# wheel runs without auth, the host connects over WS without
# X-Forwarded-Email, and all session/agent/runner routes accept
# unauthenticated requests in this mode.
export OMNIGENT_DATA_DIR="$CANARY_DATA_DIR"
export OMNIGENT_PORT="$CANARY_PORT"
export OMNIGENT_CONFIG_HOME="$CANARY_HOME/.omnigent"
export OMNIGENT_HARNESS_TMP_PARENT="$CANARY_DATA_DIR/harness-tmp"
export OMNIGENT_HARNESS_IDLE_TIMEOUT_S=3600
export OMNIGENT_TELEMETRY_ENABLED=1
export OMNIGENT_OTEL_CAPTURE_CONTENT=0
export OMNIROUTE_API_KEY="$OMNIROUTE_API_KEY"
export OMNIROUTE_BASE_URL="${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128/v1}"
export OMNIROUTE_AUTH_TOKEN="${OMNIROUTE_AUTH_TOKEN:-$OMNIROUTE_API_KEY}"
export OMNIROUTE_ROUTER_NAME=omniroute

echo "canary-run: starting wheel on port $CANARY_PORT (log: $WHEEL_LOG)"
setsid nohup "$WHEEL_BIN" server \
  --host 127.0.0.1 --port "$CANARY_PORT" --no-open \
  --config "$CANARY_DATA_DIR/config.yaml" \
  --database-uri "sqlite:///$CANARY_DATA_DIR/chat.db" \
  --artifact-location "$CANARY_DATA_DIR/artifacts" \
  --agent "$CANARY_DATA_DIR/agents/verity" \
  > "$WHEEL_LOG" 2>&1 < /dev/null &
WHEEL_PID=$!
disown "$WHEEL_PID" 2>/dev/null || true

# Wait for /health.
HEALTHY=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$CANARY_PORT/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
if [ "$HEALTHY" != 1 ]; then
  echo "canary-run: wheel failed to come up; see $WHEEL_LOG" >&2
  tail -80 "$WHEEL_LOG" >&2 || true
  pkill -9 -f '/canary-venv/bin/omni server' 2>/dev/null || true
  exit 2
fi
echo "canary-run: wheel is up on port $CANARY_PORT"

# Start a temporary host (runner) bound to the canary wheel.
#
# We set HOME=$CANARY_HOME so the host's identity file lives at
# $CANARY_HOME/.omnigent/config.yaml (NOT the operator's real
# ~/.omnigent/config.yaml). The host identity is regenerated on
# each canary run (no carry-over from previous runs).
HOST_STARTED=0
if [ "$LAUNCH_HOST" = 1 ]; then
  echo "canary-run: starting temporary host (log: $HOST_LOG)"
  HOME="$CANARY_HOME" \
  OMNIGENT_DATA_DIR="$CANARY_DATA_DIR" \
  OMNIGENT_CONFIG_HOME="$CANARY_HOME/.omnigent" \
  OMNIGENT_HARNESS_TMP_PARENT="$CANARY_DATA_DIR/harness-tmp" \
  setsid nohup "$WHEEL_BIN" host \
    --server "http://127.0.0.1:$CANARY_PORT" \
    --non-interactive \
    > "$HOST_LOG" 2>&1 < /dev/null &
  HOST_PID=$!
  disown "$HOST_PID" 2>/dev/null || true

  # Wait for the host to register. The wheel exposes the registered
  # hosts via GET /v1/hosts. We poll until at least one host appears
  # with status=online.
  REGISTERED=0
  for _ in $(seq 1 40); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$CANARY_PORT/v1/hosts" 2>/dev/null \
        | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('data',[])))" 2>/dev/null \
        | grep -qE '^[1-9][0-9]*$'; then
      REGISTERED=1
      break
    fi
    sleep 1
  done
  if [ "$REGISTERED" != 1 ]; then
    echo "canary-run: temporary host did NOT register within 40s" >&2
    echo "  wheel log: $WHEEL_LOG" >&2
    echo "  host log:  $HOST_LOG" >&2
    tail -40 "$HOST_LOG" >&2 || true
    pkill -9 -f '/canary-venv/bin/omni host'  2>/dev/null || true
    pkill -9 -f '/canary-venv/bin/omni server' 2>/dev/null || true
    exit 2
  fi
  echo "canary-run: host registered"
  HOST_STARTED=1
else
  echo "canary-run: --no-host set, skipping host launch"
fi

# Cleanup function — kills the temporary host + wheel when the
# canary finishes (PASS, FAIL, or signal). Always invoked via the
# EXIT trap so a Ctrl-C doesn't leak a half-running canary.
cleanup() {
  local exit_code=$?
  echo "canary-run: cleanup (exit=$exit_code)"
  if [ "$HOST_STARTED" = 1 ]; then
    pkill -9 -f '/canary-venv/bin/omni host'  2>/dev/null || true
  fi
  pkill -9 -f '/canary-venv/bin/omni server' 2>/dev/null || true
  return "$exit_code"
}
trap cleanup EXIT

# Forward the runner-relevant env vars and invoke the canary runner.
export CANARY_IDENTITY="${CANARY_IDENTITY:-canary@omnigent.local}"
export CANARY_FIXTURES_ROOT="${CANARY_FIXTURES_ROOT:-/tmp/canary-fixtures}"
export UNIT_NAME="${UNIT_NAME:-omnigent-canary}"
export REQUESTED_CHILD_HARNESS="${REQUESTED_CHILD_HARNESS:-pi}"
export PROD_PORT="$PROD_PORT"
export PROD_HOSTNAME="$PROD_HOSTNAME"
# The runner no longer needs X-Forwarded-Email (canary is local mode),
# but we keep the env var unset so child shell scripts that default to
# it can still observe it (some checks log the value).
unset OMNIGENT_AUTH_HEADER || true

echo "canary-run: invoking canary.sh run --run-id $RUN_ID --reuse-data-dir"
exec "$REPO_ROOT/deploy/rebuild/tests/canary.sh" run \
  --rebuild-sha "$REBUILD_SHA" \
  --run-id "$RUN_ID" \
  --reuse-data-dir