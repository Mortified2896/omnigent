#!/usr/bin/env bash
# Status / provenance report for the long-term deploy architecture.
#
# Reports:
#   - active release (the ``current`` symlink target);
#   - previous release (``previous`` symlink target);
#   - recorded ``deployed-sha`` (last successfully promoted SHA);
#   - previous-deployed-sha recorded after the latest promotion;
#   - live systemd unit state;
#   - live process PID, executable, working directory;
#   - resolved ``omnigent`` module path inside the live Python;
#   - resolved ``omnigent.server.app`` path inside the live Python;
#   - web UI bundle preflight state (present? explicit API-only?);
#   - whether all provenance checks agree (``STATUS: OK`` /
#     ``STATUS: MISMATCH``).
#
# Exit codes:
#   0  all provenance checks agree.
#   1  mismatch detected (run ``scripts/rollback_release.sh`` to
#      restore the previous release).
#   2  internal failure reading the layout (rare; treats as fatal).

set -euo pipefail

SCRIPT_NAME="deploy-status"
log() { printf '[%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
fail() { printf '[%s] FATAL: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 2; }

export DEPLOY_ROOT="${DEPLOY_ROOT:-/home/hermes/workspace/deployments/omnigent}"
export OMNIGENT_DEPLOY_ROOT="$DEPLOY_ROOT"
export PATH="/home/hermes/.local/bin:$PATH"

REPO_ROOT="${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval}"

if [[ ! -d "$DEPLOY_ROOT" ]]; then
  log "deploy root does not exist yet: $DEPLOY_ROOT"
  log "(nothing has been promoted; nothing to report)"
  exit 0
fi

CURRENT_LINK="$DEPLOY_ROOT/current"
PREVIOUS_LINK="$DEPLOY_ROOT/previous"
DEPLOYED_SHA_FILE="${DEPLOYED_SHA_FILE:-/home/hermes/.omnigent/deployed-sha}"

if [[ ! -L "$CURRENT_LINK" ]]; then
  log "WARNING: $CURRENT_LINK is not a symlink"
fi

CURRENT_SHA=""
CURRENT_DIR=""
if [[ -L "$CURRENT_LINK" ]]; then
  CURRENT_DIR=$(readlink -f "$CURRENT_LINK")
  if [[ -d "$CURRENT_DIR" ]]; then
    CURRENT_SHA=$(basename "$CURRENT_DIR")
  fi
fi

PREVIOUS_SHA=""
PREVIOUS_DIR=""
if [[ -L "$PREVIOUS_LINK" ]]; then
  PREVIOUS_DIR=$(readlink -f "$PREVIOUS_LINK")
  if [[ -d "$PREVIOUS_DIR" ]]; then
    PREVIOUS_SHA=$(basename "$PREVIOUS_DIR")
  fi
fi

RECORDED_SHA=""
if [[ -f "$DEPLOYED_SHA_FILE" ]]; then
  RECORDED_SHA=$(tr -d '[:space:]' < "$DEPLOYED_SHA_FILE")
fi

SERVICE_NAME=$(OMNIGENT_DEPLOY_SERVICE_NAME="x" python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.ops.systemd import service_name
print(service_name())
")
SERVICE_PORT=$(OMNIGENT_DEPLOY_SERVICE_PORT="x" python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.ops.systemd import service_port
print(service_port())
")

UNIT_STATE=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)

LIVE_PID=$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || echo "")
LIVE_CMD=""
LIVE_CWD=""
LIVE_EXE=""
LIVE_MODULE=""
LIVE_APP=""
if [[ -n "$LIVE_PID" ]] && [[ "$LIVE_PID" != "0" ]]; then
  LIVE_CMD=$(tr '\0' ' ' </proc/"$LIVE_PID"/cmdline 2>/dev/null || true)
  LIVE_CWD=$(readlink /proc/"$LIVE_PID"/cwd 2>/dev/null || true)
  LIVE_EXE=$(readlink /proc/"$LIVE_PID"/exe 2>/dev/null || true)
  if [[ -n "$LIVE_EXE" ]]; then
    # Resolve omnigent's __file__ inside the live process's venv via
    # /proc/<pid>/root/<venv-python>. This is the same check the
    # supervisor gate does at ExecStartPre time, so this command can
    # detect drift after the service started.
    LIVE_MODULE=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.supervisor.provenance import check_runtime_provenance
try:
    info = check_runtime_provenance(__import__('pathlib').Path('$CURRENT_DIR'))
    print(info['omnigent_module'])
except Exception as exc:
    print(f'(error: {exc})')
" 2>/dev/null || true)
    LIVE_APP=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.supervisor.provenance import check_runtime_provenance
try:
    info = check_runtime_provenance(__import__('pathlib').Path('$CURRENT_DIR'))
    print(info['omnigent_server_app'])
except Exception as exc:
    print(f'(error: {exc})')
" 2>/dev/null || true)
  fi
fi

# Web UI bundle state (per release).
WEB_BUNDLE_STATE="unknown"
if [[ -n "$CURRENT_DIR" ]] && [[ -d "$CURRENT_DIR" ]]; then
  if "$CURRENT_DIR/.venv/bin/python" -m omnigent.deploy.preflight "$CURRENT_DIR" \
       >/dev/null 2>&1; then
    WEB_BUNDLE_STATE="present"
  elif [[ "${OMNIGENT_SKIP_WEB_UI:-false}" == "true" ]] \
       || systemctl show "$SERVICE_NAME" -p Environment --value 2>/dev/null \
            | grep -q OMNIGENT_SKIP_WEB_UI=true; then
    WEB_BUNDLE_STATE="absent (explicit API-only)"
  else
    WEB_BUNDLE_STATE="absent (UNEXPECTED for a UI deployment)"
  fi
fi

LOOPBACK_HEALTH=$(curl -fsS -m 5 "http://127.0.0.1:$SERVICE_PORT/health" 2>/dev/null \
                  | head -c 60 || echo "(unreachable)")

# Determine if all provenance checks agree.
STATUS="OK"
REASONS=()
if [[ -z "$CURRENT_SHA" ]]; then
  STATUS="NO_RELEASE"
  REASONS+=("current symlink missing or invalid")
fi
if [[ -z "$RECORDED_SHA" ]] && [[ "$STATUS" != "NO_RELEASE" ]]; then
  STATUS="MISMATCH"
  REASONS+=("deployed-sha missing")
elif [[ -n "$RECORDED_SHA" ]] && [[ -n "$CURRENT_SHA" ]] \
     && [[ "$RECORDED_SHA" != "$CURRENT_SHA" ]]; then
  STATUS="MISMATCH"
  REASONS+=("deployed-sha=$RECORDED_SHA but current=$CURRENT_SHA")
fi
if [[ "$UNIT_STATE" != "active" ]]; then
  STATUS="MISMATCH"
  REASONS+=("systemd unit $SERVICE_NAME is $UNIT_STATE")
fi
if [[ -n "$LIVE_EXE" ]] && [[ -n "$CURRENT_DIR" ]]; then
  case "$LIVE_EXE" in
    "$CURRENT_DIR"/*) ;;  # ok
    *)
      STATUS="MISMATCH"
      REASONS+=("live exe=$LIVE_EXE is not under current release=$CURRENT_DIR")
      ;;
  esac
fi
if [[ "$WEB_BUNDLE_STATE" == "absent (UNEXPECTED for a UI deployment)" ]]; then
  STATUS="MISMATCH"
  REASONS+=("web UI bundle is missing on a UI deployment")
fi

cat <<STATUS
deploy_status
  deploy_root:           $DEPLOY_ROOT
  current_link:          $CURRENT_LINK -> $CURRENT_DIR
  current_sha:           ${CURRENT_SHA:-(none)}
  previous_link:         $PREVIOUS_LINK -> $PREVIOUS_DIR
  previous_sha:          ${PREVIOUS_SHA:-(none)}
  recorded_deployed_sha: ${RECORDED_SHA:-(none)}
  service:               $SERVICE_NAME
  unit_state:            $UNIT_STATE
  service_port:          $SERVICE_PORT
  live_pid:              ${LIVE_PID:-0}
  live_exe:              ${LIVE_EXE:-(unknown)}
  live_cwd:              ${LIVE_CWD:-(unknown)}
  live_command:          ${LIVE_CMD:-(unknown)}
  live_omnigent_module:  ${LIVE_MODULE:-(n/a)}
  live_server_app:       ${LIVE_APP:-(n/a)}
  web_ui_bundle:         $WEB_BUNDLE_STATE
  loopback_health:       $LOOPBACK_HEALTH
  STATUS: $STATUS
STATUS

if [[ "$STATUS" == "OK" ]]; then
  exit 0
fi
if [[ ${#REASONS[@]} -gt 0 ]]; then
  log "reasons:"
  for r in "${REASONS[@]}"; do log "  - $r"; done
fi
exit 1
