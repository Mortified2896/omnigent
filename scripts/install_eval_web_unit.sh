#!/usr/bin/env bash
# Install/refresh the stable base systemd unit for omnigent-eval-web.
#
# The unit file is intentionally minimal: it sets the user/environment
# and a ``Wants`` / ``After`` network dependency. Everything
# deployment-specific lives in the ``10-release-<sha>.conf`` drop-in
# that ``scripts/promote_release.sh`` writes. An LLM agent that
# hand-edits the unit file can only break the *base* (no release
# pinned), not bypass the supervisor gate (which the drop-in
# provides via ``OMNIGENT_RELEASE_DIR``).
#
# The ``ExecStartPre=`` runs the supervisor gate as the release's
# own Python interpreter; doing so means the gate imports ``omnigent``
# from inside the release venv, not from the calling agent's shell.
#
# Usage:
#   scripts/install_eval_web_unit.sh
#
# Idempotent: re-running the script writes the same file with the
# same content; ``daemon-reload`` is called only when the file
# actually changed.

set -euo pipefail

SCRIPT_NAME="install-eval-web-unit"
log() { printf '[%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
fail() { printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

UNIT_PATH="/etc/systemd/system/omnigent-eval-web.service"
DROPIN_DIR="/etc/systemd/system/omnigent-eval-web.service.d"

REPO_ROOT="${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval}"
if [[ ! -d "$REPO_ROOT" ]]; then
  fail "REPO_ROOT does not exist: $REPO_ROOT"
fi

# Pull defaults from the python helper (kept in sync with the
# deployment scripts via ``OMNIGENT_DEPLOY_*`` env vars).
SERVICE_NAME=$(sudo /home/hermes/.local/bin/python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.ops.systemd import service_name
print(service_name())
") || SERVICE_NAME="omnigent-eval-web.service"
SERVICE_PORT=$(sudo /home/hermes/.local/bin/python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from omnigent.deploy.ops.systemd import service_port
print(service_port())
") || SERVICE_PORT=4097

# The ``ExecStartPre=`` resolves the release's venv python from the
# ``OMNIGENT_RELEASE_DIR`` env var the drop-in sets. This is the only
# pre-start dependency the unit has on a release being present —
# without a ``10-release-<sha>.conf`` drop-in, the unit will refuse
# to start.
read -r -d '' UNIT <<'UNIT_EOF' || true
[Unit]
Description=Omnigent Eval Web UI (isolated Tailscale evaluation)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=hermes
Group=hermes
Environment=HOME=/home/hermes
Environment=PATH=/home/hermes/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=OMNIGENT_WS_ALLOWED_ORIGINS=https://hermes-agent.taile0361b.ts.net
Environment=OMNIGENT_ACCOUNTS_BASE_URL=https://hermes-agent.taile0361b.ts.net:9461
# ExecStart is set by the active 10-release-<sha>.conf drop-in. If
# no drop-in is present, the unit will fail on the pre-start gate in
# that drop-in (the gate reads OMNIGENT_RELEASE_DIR from the unit's
# environment and refuses when the release cannot prove its
# provenance). ``/bin/false`` here is the placeholder before any
# drop-in is wired up; after a successful ``promote_release.sh`` it
# points at the release's ``.venv/bin/python``.
ExecStart=/bin/false
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT_EOF

TMP_PATH=$(mktemp)
printf '%s\n' "$UNIT" > "$TMP_PATH"
chmod 0644 "$TMP_PATH"

if [[ -f "$UNIT_PATH" ]] && diff -q "$UNIT_PATH" "$TMP_PATH" >/dev/null 2>&1; then
  log "unit file is up-to-date; not regenerating"
  rm -f "$TMP_PATH"
else
  log "writing $UNIT_PATH"
  sudo install -m 0644 -o root -g root "$TMP_PATH" "$UNIT_PATH"
  rm -f "$TMP_PATH"
  CHANGED=1
fi

# Ensure drop-in dir exists.
if [[ ! -d "$DROPIN_DIR" ]]; then
  log "creating $DROPIN_DIR"
  sudo mkdir -p "$DROPIN_DIR"
fi

if [[ "${CHANGED:-0}" == "1" ]]; then
  log "running systemctl daemon-reload"
  sudo systemctl daemon-reload || fail "systemctl daemon-reload failed"
fi

log "unit file in place; per-release drop-in is managed by scripts/promote_release.sh"
