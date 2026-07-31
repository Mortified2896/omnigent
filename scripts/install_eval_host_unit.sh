#!/usr/bin/env bash
# Install/refresh the stable base systemd unit for omnigent-eval-host.
#
# This is the canonical companion to ``install_eval_web_unit.sh``.
# The host daemon (``omni host --server http://127.0.0.1:<port>``)
# connects to the local web service over loopback. Both services are
# pinned to the same release by ``scripts/promote_release.sh`` —
# this script only installs the base unit; the per-release drop-in is
# managed by the promotion script.
#
# The unit file is intentionally minimal: it sets the user/environment
# and a ``Wants`` / ``After`` dependency on the web service. Everything
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
# Host-environment-only drop-ins (router-env.conf,
# minimax-token-plan.conf, tailscale-origin.conf) live under the
# service's drop-in directory and are managed by hand. They are
# preserved by this script: the base unit does not touch them, and
# the promotion script only manipulates ``10-release-<sha>.conf``
# files. A future operator who wants to migrate the host service to
# a fresh machine can copy those drop-ins in place before running
# the promotion script.
#
# Usage:
#   scripts/install_eval_host_unit.sh
#
# Idempotent: re-running the script writes the same file with the
# same content; ``daemon-reload`` is called only when the file
# actually changed.

set -euo pipefail

SCRIPT_NAME="install-eval-host-unit"
log() { printf '[%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
fail() { printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

UNIT_PATH="/etc/systemd/system/omnigent-eval-host.service"
DROPIN_DIR="/etc/systemd/system/omnigent-eval-host.service.d"

REPO_ROOT="${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval}"
if [[ ! -d "$REPO_ROOT" ]]; then
  fail "REPO_ROOT does not exist: $REPO_ROOT"
fi

# Pin to the web service so the host daemon never starts before the
# web service is reachable on loopback. The web service's drop-in
# already binds ``--host 127.0.0.1``; if the host service races ahead
# and tries to register before the server has bound, ``omni host``
# falls back to a retry loop that masks the real startup failure.
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
#
# ExecStart is set by the active 10-release-<sha>.conf drop-in. If
# no drop-in is present, the unit will fail on the pre-start gate in
# that drop-in (the gate reads OMNIGENT_RELEASE_DIR from the unit's
# environment and refuses when the release cannot prove its
# provenance). ``/bin/false`` here is the placeholder before any
# drop-in is wired up; after a successful ``promote_release.sh`` it
# points at the release's ``.venv/bin/omni``.
#
# ExecStopPost is also set by the host drop-in — the per-release
# drop-in installs ``ExecStopPost=...omni host stop --server ...``
# so a clean ``systemctl stop`` invokes the release's own ``omni``
# binary, not whatever happens to be on PATH. The placeholder
# ``/bin/true`` here is a no-op fallback used only when no drop-in
# is present.
read -r -d '' UNIT <<'UNIT_EOF' || true
[Unit]
Description=Omnigent Eval Host (registers this machine for remote sessions)
# Pin host startup to the web service coming up first. The web
# service's drop-in already binds ``--host 127.0.0.1``; starting the
# host daemon before then leads to a startup race where ``omni host``
# retries against a port that has nothing listening and surfaces as
# an opaque connection-refused error in the journal.
After=network-online.target tailscaled.service omnigent-eval-web.service
Wants=network-online.target omnigent-eval-web.service

[Service]
Type=simple
User=hermes
Group=hermes
Environment=HOME=/home/hermes
Environment=PATH=/home/hermes/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/bin/false
ExecStopPost=/bin/true
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

# Ensure drop-in dir exists. Host-environment-only drop-ins
# (``router-env.conf``, ``minimax-token-plan.conf``,
# ``tailscale-origin.conf``) are managed by hand and are preserved
# by this script — we only ensure the directory exists; we do not
# touch existing drop-ins.
if [[ ! -d "$DROPIN_DIR" ]]; then
  log "creating $DROPIN_DIR"
  sudo mkdir -p "$DROPIN_DIR"
fi

if [[ "${CHANGED:-0}" == "1" ]]; then
  log "running systemctl daemon-reload"
  sudo systemctl daemon-reload || fail "systemctl daemon-reload failed"
fi

log "unit file in place; per-release drop-in is managed by scripts/promote_release.sh"
log "host-environment-only drop-ins under $DROPIN_DIR are preserved (managed by hand)"