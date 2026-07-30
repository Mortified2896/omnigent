#!/usr/bin/env bash
# Roll back the omnigent-eval-web service to the previous known-good release.
#
# Reads the ``previous`` symlink in the deploy root, atomically swaps
# ``current`` to it, rewrites the systemd drop-in to point at the
# previous release, asks systemd to restart, and verifies the live
# service is healthy before exiting. The recorded ``deployed-sha`` is
# rewritten to the rolled-back SHA *only after* the live health probe
# passes, so an operator can detect a broken rollback.
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
# ``previous`` overwrite cannot redirect the systemd unit.
write_dropin() {
  # The wrapper handles both write and disable; the wrapper
  # validates SHA + release-dir path before invoking the
  # release's python.
  sudo /opt/omnigent/updater/bin/write-dropin.sh write "$TARGET_SHA" "$TARGET"
}
DROPIN_PATH=$(write_dropin) || fail "could not write drop-in (sudo required)"

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

# Loopback probe.
if body=$(curl -fsS -m 5 "http://127.0.0.1:$SERVICE_PORT/health" 2>/dev/null); then
  if [[ -z "$body" ]]; then
    echo "loopback health returned empty body" >> "$FAILED_DIR/info.txt"
    fail "loopback health probe returned empty"
  fi
else
  echo "loopback health probe failed" >> "$FAILED_DIR/info.txt"
  fail "loopback /health probe failed"
fi

if body=$(curl -fsS -m 5 "http://127.0.0.1:$SERVICE_PORT/" 2>/dev/null); then
  if printf '%s' "$body" | grep -q 'OMNIGENT_SKIP_WEB_UI'; then
    fail "rolled-back release serves the API-only landing page; bailing"
  fi
else
  fail "loopback / probe failed"
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
