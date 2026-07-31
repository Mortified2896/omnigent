#!/usr/bin/env bash
# Roll back the omnigent-eval-web and omnigent-eval-host services to
# the previous known-good release.
#
# Reads the ``previous`` symlink in the deploy root, atomically swaps
# ``current`` to it, rewrites the systemd drop-ins for **both** the
# web and host services to point at the previous release, asks systemd
# to restart the web service first, then restarts the host daemon and
# verifies both services are healthy. The recorded ``deployed-sha`` is
# rewritten to the rolled-back SHA *only after* the live health probes
# pass, so an operator can detect a broken rollback.
#
# The host daemon is pinned to the same release as the web service;
# this script is the single point of truth for the coordinated
# web+host rollback. An LLM agent or operator that calls into the
# sudoers-gated ``write-dropin.sh`` directly cannot change one service
# without going through this script's coordinated flow.
#
# Usage:
#   scripts/rollback_release.sh [--to <sha>]
#
# Without --to, rolls back to the symlinked ``previous`` release.
# With --to, points the service at an arbitrary historical SHA that
# must already be present in the deploy root's ``releases/`` tree.

set -euo pipefail

SCRIPT_NAME="rollback-release"
log() { printf '[%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
fail() { printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

export DEPLOY_ROOT="${DEPLOY_ROOT:-/home/hermes/workspace/deployments/omnigent}"
export OMNIGENT_DEPLOY_ROOT="$DEPLOY_ROOT"
export PATH="/home/hermes/.local/bin:$PATH"

REPO_ROOT="${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval}"
CURRENT_LINK="$DEPLOY_ROOT/current"
PREVIOUS_LINK="$DEPLOY_ROOT/previous"

EXPLICIT_TARGET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)
      EXPLICIT_TARGET="$2"
      shift 2
      ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--to <sha>]
USAGE
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

# Capture the previous-release identity up front so later symlink
# mutations cannot redirect the drop-in / ``current`` switch / logs.
if [[ -n "$EXPLICIT_TARGET" ]]; then
  TARGET="$DEPLOY_ROOT/releases/$EXPLICIT_TARGET"
  [[ -d "$TARGET" ]] || fail "release directory does not exist: $TARGET"
  TARGET_SHA="$EXPLICIT_TARGET"
elif [[ -L "$PREVIOUS_LINK" ]]; then
  TARGET=$(readlink -f "$PREVIOUS_LINK")
  TARGET_SHA=$(basename "$TARGET")
else
  fail "no previous symlink at $PREVIOUS_LINK; cannot roll back"
fi
SHORT_SHA="${TARGET_SHA:0:12}"
log "rolling back to $TARGET_SHA (target dir: $TARGET)"

# Snapshot the current state for forensics on failure.
FAILED_DIR="$DEPLOY_ROOT/failed/rollback-$TARGET_SHA-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$FAILED_DIR"
{
  echo "rollback started at $(date -u --iso-8601=seconds)"
  echo "current -> $(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
  echo "previous -> $(readlink -f "$PREVIOUS_LINK" 2>/dev/null || true)"
  echo "target: $TARGET"
  echo "target_sha: $TARGET_SHA"
} > "$FAILED_DIR/info.txt"

# Drop-in rewrite uses TARGET/TARGET_SHA captured above so a later
# ``previous`` overwrite cannot redirect the systemd unit. Both
# services (web + host) are pinned to the same release, so we write
# drop-ins for both before restarting anything.
write_dropin() {
  # The wrapper handles both write and disable; the wrapper
  # validates SHA + release-dir path before invoking the
  # release's python.
  local kind="$1"
  sudo /opt/omnigent/updater/bin/write-dropin.sh write "$kind" "$TARGET_SHA" "$TARGET"
}
WEB_DROPIN_PATH=$(write_dropin web) || fail "could not write omnigent-eval-web.service drop-in (sudo required)"
log "  web drop-in: $WEB_DROPIN_PATH"
HOST_DROPIN_PATH=$(write_dropin host) || fail "could not write omnigent-eval-host.service drop-in (sudo required)"
log "  host drop-in: $HOST_DROPIN_PATH"
DROPIN_PATH="$WEB_DROPIN_PATH"

# Atomic current-symlink swap (mv -T atomically replaces).
ln -s "$TARGET" "$DEPLOY_ROOT/.current.new.$SHORT_SHA"
mv -T "$DEPLOY_ROOT/.current.new.$SHORT_SHA" "$CURRENT_LINK"
log "current -> $TARGET"

# daemon-reload + restart (the live service has no idea the symlink
# changed; the drop-in update is the actual configuration change).
# Unset the override env vars so the helpers fall back to their
# built-in defaults instead of inheriting a caller-set literal.
unset OMNIGENT_DEPLOY_SERVICE_NAME OMNIGENT_DEPLOY_SERVICE_PORT
SERVICE_NAME=$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.ops.systemd import service_name
print(service_name())
")
SERVICE_PORT=$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.ops.systemd import service_port
print(service_port())
")
HOST_SERVICE_NAME=$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.ops.systemd import host_service_spec
print(host_service_spec().service_name)
")

sudo systemctl daemon-reload || fail "systemctl daemon-reload failed"
sudo systemctl restart "$SERVICE_NAME" || fail "systemctl restart $SERVICE_NAME failed"

# Wait for active state.
up_after=0
for i in $(seq 1 30); do
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    up_after=$i
    break
  fi
  sleep 2
done
if [[ "$up_after" -eq 0 ]]; then
  echo "service did not become active" >> "$FAILED_DIR/info.txt"
  systemctl status --no-pager "$SERVICE_NAME" > "$FAILED_DIR/systemctl-status.txt" 2>&1 || true
  journalctl -n 200 --no-pager -u "$SERVICE_NAME" > "$FAILED_DIR/journal.txt" 2>&1 || true
  fail "service $SERVICE_NAME did not become active after restart"
fi

# Restart the host daemon AFTER the web side settles. The host
# daemon has no HTTP surface, so its health check is process-level:
# verify the running executable is inside the rolled-back release's
# ``.venv`` (not the mutable repository checkout).
log "restarting $HOST_SERVICE_NAME (host)"
sudo systemctl restart "$HOST_SERVICE_NAME" || fail "systemctl restart $HOST_SERVICE_NAME failed"

host_up_after=0
for i in $(seq 1 30); do
  if systemctl is-active --quiet "$HOST_SERVICE_NAME"; then
    host_up_after=$i
    break
  fi
  sleep 2
done
if [[ "$host_up_after" -eq 0 ]]; then
  echo "host service did not become active" >> "$FAILED_DIR/info.txt"
  systemctl status --no-pager "$HOST_SERVICE_NAME" > "$FAILED_DIR/host-systemctl-status.txt" 2>&1 || true
  journalctl -n 200 --no-pager -u "$HOST_SERVICE_NAME" > "$FAILED_DIR/host-journal.txt" 2>&1 || true
  fail "host service $HOST_SERVICE_NAME did not become active after restart"
fi

HOST_EXE=$(readlink "/proc/$(systemctl show -p MainPID --value "$HOST_SERVICE_NAME")/exe" 2>/dev/null || echo "")
case "$HOST_EXE" in
  "$TARGET"/.venv/*) log "host daemon running from $HOST_EXE (release-pinned to $TARGET_SHA)" ;;
  *) echo "host daemon executable $HOST_EXE is NOT inside $TARGET/.venv" >> "$FAILED_DIR/info.txt"
     systemctl status --no-pager "$HOST_SERVICE_NAME" > "$FAILED_DIR/host-systemctl-status.txt" 2>&1 || true
     journalctl -n 200 --no-pager -u "$HOST_SERVICE_NAME" > "$FAILED_DIR/host-journal.txt" 2>&1 || true
     fail "host daemon is running from $HOST_EXE, not $TARGET/.venv/..." ;;
esac

# Loopback probe. systemd reports ``active`` before uvicorn binds the
# loopback socket, so a single curl right after ``systemctl restart``
# can race the app startup and return ``Connection refused`` even
# though the rolled-back release is healthy. Retry the probe in a
# short loop (matches the prometheus-style convention the live
# service already uses for its readiness signal).
loopback_probe() {
  local url="$1"
  local attempts="${2:-30}"
  local sleep_s="${3:-1}"
  for ((i = 0; i < attempts; i++)); do
    if body=$(curl -fsS -m 5 "$url" 2>/dev/null) && [[ -n "$body" ]]; then
      printf '%s' "$body"
      return 0
    fi
    sleep "$sleep_s"
  done
  return 1
}

if ! body=$(loopback_probe "http://127.0.0.1:$SERVICE_PORT/health"); then
  echo "loopback /health probe failed after retries" >> "$FAILED_DIR/info.txt"
  fail "loopback /health probe failed"
fi

if ! body=$(loopback_probe "http://127.0.0.1:$SERVICE_PORT/"); then
  echo "loopback / probe failed after retries" >> "$FAILED_DIR/info.txt"
  fail "loopback / probe failed"
fi

if printf '%s' "$body" | grep -q 'OMNIGENT_SKIP_WEB_UI'; then
  fail "rolled-back release serves the API-only landing page; bailing"
fi

# Public probe (best-effort).
if body=$(curl -fsS -m 8 "https://hermes-agent.taile0361b.ts.net:9461/" 2>/dev/null); then
  if printf '%s' "$body" | grep -q 'OMNIGENT_SKIP_WEB_UI'; then
    fail "public probe serves the API-only landing page; rolled-back release is broken"
  fi
fi

# Update deployment-sha on success.
# Source the shared live-SHA helper so the rollback writes the
# canonical marker the external updater reads. The helper honors
# OMNIGENT_DEPLOYED_SHA_FILE / OMNIGENT_DEPLOYED_SHA_DIR and falls
# back to /var/lib/omnigent/shared/ when writable.
SCRIPT_DIR_DEPLOYED_SHA_HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_deployed_sha.sh
source "$SCRIPT_DIR_DEPLOYED_SHA_HELPER/_deployed_sha.sh"
_deployed_sha_write_current "$TARGET_SHA"
log "rolled back to $TARGET_SHA; deployed-sha=$DEPLOYED_SHA_FILE updated"

rm -rf "$FAILED_DIR"
exit 0
