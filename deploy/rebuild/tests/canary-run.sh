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
#
# The host process reads this on connect to resolve provider
# credentials for Pi / OpenCode harness subprocesses. The Pi /
# OpenCode readiness checks read the same config to decide whether
# each harness is "ready" (configured) vs "needs-auth".
#
# Provider shape follows omnigent/onboarding/provider_config.py:
#   kind: gateway  with an `openai:` family block (Pi / OpenCode
#   consume the openai family). wire_api: chat because OmniRoute
#   does not implement the OpenAI Responses API (the codex executor
#   would otherwise default to that and fail). api_key_ref:
#   env:OMNIROUTE_API_KEY resolves the secret from the wheel /
#   host's environment at family-read time, never written to disk.
#
# `default: [openai, pi]` makes this the default for the openai
# family AND the pi surface (Pi consumes either family and prefers
# anthropic then openai — see _PI_FALLBACK_FAMILIES). Without the
# explicit `pi` scope, the per-family default alone wouldn't claim
# pi and the readiness check would still report "needs-auth".
#
# Model: auto/best-coding (the same canonical auto/route the
# operator's opencode.jsonc uses for coding work). The base URL is
# OmniRoute's OpenAI-compatible surface; Pi / OpenCode speak to it
# as a generic gateway (no silent fallback, no per-call rewrite).
cat > "$CANARY_HOME/.omnigent/config.yaml" <<YAML
providers:
  omniroute:
    kind: gateway
    default: [openai, pi]
    openai:
      base_url: ${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128/v1}
      api_key_ref: env:OMNIROUTE_API_KEY
      wire_api: chat
      models:
        default: auto/best-coding
    anthropic:
      base_url: ${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128/v1}
      api_key_ref: env:OMNIROUTE_API_KEY
      models:
        default: auto/claude-sonnet

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
#
# HOME is set to $CANARY_HOME so the wheel's own CLI logs (under
# $HOME/.omnigent/logs/cli/) go to the canary data dir, not the
# operator's real ~/.omnigent/ (which on this VM is read-only).
export HOME="$CANARY_HOME"
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
# Allow the host to forward these into runner subprocess env so
# Pi / OpenCode harness invocations see the same OmniRoute
# credential as the wheel (used by user-config's api_key_ref:
# env:OMNIROUTE_API_KEY to resolve the provider secret). The
# host's env allowlist deliberately excludes random env vars to
# avoid leaking the operator's full env to runners; the explicit
# passthrough is the operator knob for that.
export OMNIGENT_RUNNER_ENV_PASSTHROUGH="OMNIROUTE_API_KEY,OMNIROUTE_BASE_URL,OMNIROUTE_AUTH_TOKEN,OMNIROUTE_ROUTER_NAME"

# Langfuse OTEL wiring (reuses the same tenant the operator's
# OpenCode Web uses, with a distinct environment tag so canary
# traces never collide with the operator's normal OpenCode
# traffic). Credentials are read from /etc/hermes/hermes.env (the
# operator's single source of truth, mode 0640 root:hermes). They
# are passed to the wheel + host via env vars; nothing is ever
# written to disk.
#
# If /etc/hermes/hermes.env is missing or unreadable, the canary
# still runs but check 10 (Langfuse) will FAIL with a clear reason
# (it cannot query the API without credentials).
LANGFUSE_ENV_FILE="${LANGFUSE_ENV_FILE:-/etc/hermes/hermes.env}"
if [ -r "$LANGFUSE_ENV_FILE" ]; then
  # Read without eval: only set the names we use, and only if the
  # operator has not already exported them. No values are logged.
  while IFS= read -r line; do
    case "$line" in
      ""|\#*) continue ;;
    esac
    case "$line" in
      HERMES_LANGFUSE_PUBLIC_KEY=*)
        if [ -z "${LANGFUSE_PUBLIC_KEY:-}" ]; then
          export LANGFUSE_PUBLIC_KEY="${line#HERMES_LANGFUSE_PUBLIC_KEY=}"
        fi ;;
      HERMES_LANGFUSE_SECRET_KEY=*)
        if [ -z "${LANGFUSE_SECRET_KEY:-}" ]; then
          export LANGFUSE_SECRET_KEY="${line#HERMES_LANGFUSE_SECRET_KEY=}"
        fi ;;
      HERMES_LANGFUSE_BASE_URL=*)
        if [ -z "${LANGFUSE_BASE_URL:-}" ]; then
          export LANGFUSE_BASE_URL="${line#HERMES_LANGFUSE_BASE_URL=}"
        fi ;;
      HERMES_LANGFUSE_ENV=*)
        if [ -z "${LANGFUSE_ENV:-}" ]; then
          export LANGFUSE_ENV="${line#HERMES_LANGFUSE_ENV=}"
        fi ;;
    esac
  done <"$LANGFUSE_ENV_FILE"
fi
# Distinct canary environment tag so canary traces never collide
# with the operator's normal OpenCode traffic on the shared tenant.
# If the operator's hermes.env already set LANGFUSE_ENV, prefer
# that; otherwise default to a run-scoped tag.
if [ -z "${LANGFUSE_ENV:-}" ]; then
  export LANGFUSE_ENV="canary-${RUN_ID}"
else
  export LANGFUSE_ENV="${LANGFUSE_ENV}-canary-${RUN_ID}"
fi
export OTEL_SERVICE_NAME="omnigent-canary-${RUN_ID}"

# Langfuse v3 accepts OTLP/HTTP at <host>/api/public/otel with a
# Basic auth header (public-key:secret-key base64-encoded). The
# OTEL_EXPORTER_OTLP_HEADERS value is URL-encoded key=value pairs;
# the Authorization value is the basic header with %20 for the
# space between "Basic" and the token (HTTP-header value rules).
if [ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ] && [ -n "${LANGFUSE_BASE_URL:-}" ]; then
  _lf_basic=$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 -w0 2>/dev/null || printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64)
  export OTEL_EXPORTER_OTLP_ENDPOINT="${LANGFUSE_BASE_URL%/}/api/public/otel"
  export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20${_lf_basic}"
  # For check 10 (which queries the Langfuse REST API directly).
  export LANGFUSE_HOST="$LANGFUSE_BASE_URL"
  export CANARY_LANGFUSE_ENV="$LANGFUSE_ENV"
  echo "canary-run: langfuse OTEL wired (host=$LANGFUSE_BASE_URL env=$LANGFUSE_ENV)"
fi

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
  # with status=online. (GET /v1/hosts returns {"hosts": [...]};
  # the previous canary-run orchestrator polled the wrong field name
  # "data", which never matched the response shape, so the wait
  # always timed out. We accept either field name for compatibility
  # with future wire changes.)
  REGISTERED=0
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$CANARY_PORT/v1/hosts" 2>/dev/null \
        | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
hosts = d.get('hosts') or d.get('data') or []
print(len(hosts))
" 2>/dev/null | grep -qE '^[1-9][0-9]*$'; then
      REGISTERED=1
      break
    fi
    sleep 1
  done
  if [ "$REGISTERED" != 1 ]; then
    echo "canary-run: temporary host did NOT register within 60s" >&2
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