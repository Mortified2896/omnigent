#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# canary-run.sh — Phase D canary orchestrator for the existing VM
# ─────────────────────────────────────────────────────────────────────
#
# Boots the rebuild's 0.7 wheel against a fresh empty temp data
# dir, waits for it to come up, runs the 12-check canary, and
# captures the per-run report under
# docs/rebuild/canary-runs/<run-id>/. The wheel is left running
# for the operator's inspection.
#
# Production is NOT touched:
#   - The canary wheel binds 127.0.0.1:17670 (an unused temp port).
#   - The canary data dir is /tmp/canary-data (NOT the production
#     /home/<user>/.omnigent/chat.db or /var/lib/omnigent/chat.db).
#   - The canary env reads the local OmniRoute at 20128 (NOT the
#     production server at 4097).
#   - The canary does NOT install over the production wheel.
#   - The canary does NOT modify the production systemd unit.
#
# This is the recommended Phase D entrypoint: it orchestrates the
# wheel + the checks so the canary is reproducible on any VM
# with the build artifacts present.
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
#
# Usage:
#   ./canary-run.sh
#   RUN_ID=D-20260101T120000Z ./canary-run.sh
#   ./canary-run.sh --reuse-data-dir   # keep existing canary DB
#
# Exit codes:
#   0  all 12 checks PASS (Phase E authorized)
#   1  at least one check FAIL or required SKIPPED

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

# The remaining env vars the canary runner forwards to its
# checks (LANGFUSE_*, REQUESTED_CHILD_HARNESS, etc.) are taken
# from the inherited environment.

REUSE_DATA_DIR=0
for arg in "$@"; do
  case "$arg" in
    --reuse-data-dir) REUSE_DATA_DIR=1 ;;
    *) ;;
  esac
done

echo "canary-run: starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Stop any prior canary wheel (anchored pattern, never matches
# bash — see the AGENTS.md pgrep note).
pkill -9 -f '/canary-venv/bin/omni server' 2>/dev/null || true
sleep 2

# Fresh data dir unless the operator passed --reuse-data-dir.
if [ "$REUSE_DATA_DIR" = 0 ]; then
  echo "canary-run: wiping temp data dir $CANARY_DATA_DIR"
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

# Start the canary wheel (fully detached, survives this script's exit).
export OMNIGENT_DATA_DIR="$CANARY_DATA_DIR"
export OMNIGENT_PORT="$CANARY_PORT"
export OMNIGENT_AUTH_HEADER=X-Forwarded-Email
export OMNIGENT_CONFIG_HOME="$CANARY_HOME/.omnigent"
export OMNIGENT_HARNESS_TMP_PARENT="$CANARY_DATA_DIR/harness-tmp"
export OMNIGENT_HARNESS_IDLE_TIMEOUT_S=3600
export OMNIGENT_AUTH_PROVIDER=header
export OMNIGENT_AUTH_ENABLED=1
export OMNIGENT_TELEMETRY_ENABLED=1
export OMNIGENT_OTEL_CAPTURE_CONTENT=0
export OMNIROUTE_API_KEY="$OMNIROUTE_API_KEY"
export OMNIROUTE_BASE_URL="${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128/v1}"
export OMNIROUTE_AUTH_TOKEN="${OMNIROUTE_AUTH_TOKEN:-$OMNIROUTE_API_KEY}"
export OMNIROUTE_ROUTER_NAME=omniroute
export PATH="/home/hermes/.local/bin:/home/hermes/.hermes/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

echo "canary-run: starting wheel on port $CANARY_PORT"
setsid nohup "$WHEEL_BIN" server \
  --host 127.0.0.1 --port "$CANARY_PORT" --no-open \
  --config "$CANARY_DATA_DIR/config.yaml" \
  --database-uri "sqlite:///$CANARY_DATA_DIR/chat.db" \
  --artifact-location "$CANARY_DATA_DIR/artifacts" \
  --agent "$CANARY_DATA_DIR/agents/verity" \
  > /tmp/canary-server.log 2>&1 < /dev/null &
disown

# Wait for /health.
HEALTHY=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$CANARY_PORT/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
[ "$HEALTHY" = 1 ] || {
  echo "canary-run: wheel failed to come up; see /tmp/canary-server.log" >&2
  tail -50 /tmp/canary-server.log >&2 || true
  exit 1
}
echo "canary-run: wheel is up on port $CANARY_PORT"

# Forward the runner-relevant env vars and invoke the canary runner.
export CANARY_IDENTITY="${CANARY_IDENTITY:-canary@omnigent.local}"
export CANARY_FIXTURES_ROOT="${CANARY_FIXTURES_ROOT:-/tmp/canary-fixtures}"
export UNIT_NAME="${UNIT_NAME:-omnigent-canary}"
export REQUESTED_CHILD_HARNESS="${REQUESTED_CHILD_HARNESS:-pi}"
export PROD_PORT="$PROD_PORT"
export PROD_HOSTNAME="$PROD_HOSTNAME"

REUSE_FLAG=""
[ "$REUSE_DATA_DIR" = 1 ] && REUSE_FLAG="--reuse-data-dir"

echo "canary-run: invoking canary.sh run --run-id $RUN_ID $REUSE_FLAG"

# The runner always passes --reuse-data-dir because the wheel
# has already booted against a fresh dir (this orchestrator
# created and booted the wheel; the wheel did its first-boot
# migration). The canary.sh's check 1 then verifies that
# schema/alembic_version against the live wheel's DB.
#
# If the operator passed --reuse-data-dir AND the canary-run
# orchestrator did NOT wipe, the canary uses the existing
# wheel's state (which may or may not be a freshly-booted DB).
exec "$REPO_ROOT/deploy/rebuild/tests/canary.sh" run \
  --rebuild-sha "$REBUILD_SHA" \
  --run-id "$RUN_ID" \
  --reuse-data-dir