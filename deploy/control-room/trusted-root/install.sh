#!/usr/bin/env bash
# Install the opt-in Control Room trusted-root host policy.
#
# This is deliberately separate from generic Omnigent installation. It writes
# only the two named Control Room host drop-ins, their non-secret environment
# fragments, and the validated hermes sudoers fragment.

set -euo pipefail

POLICY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DESTDIR="${DESTDIR:-}"
SYSTEMD_DIR="${CONTROL_ROOM_SYSTEMD_DIR:-${DESTDIR}/etc/systemd/system}"
O1_ENV_DIR="${CONTROL_ROOM_O1_ENV_DIR:-${DESTDIR}/etc/omnigent}"
O2_ENV_DIR="${CONTROL_ROOM_O2_ENV_DIR:-${DESTDIR}/etc/omnigent-production}"
SUDOERS_DIR="${CONTROL_ROOM_SUDOERS_DIR:-${DESTDIR}/etc/sudoers.d}"
VISUDO="${VISUDO:-$(command -v visudo || true)}"
if [[ -z "$VISUDO" ]]; then
  for candidate in /usr/sbin/visudo /sbin/visudo; do
    if [[ -x "$candidate" ]]; then
      VISUDO="$candidate"
      break
    fi
  done
fi

usage() {
  printf 'usage: %s --check | --install\n' "$0" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

validate_sudoers() {
  [[ -n "$VISUDO" ]] || die "visudo is required to validate the sudoers fragment"
  local candidate
  candidate="$(mktemp)"
  cp "$POLICY_DIR/sudoers/99-omnigent-agent-root" "$candidate"
  # Validate the generated candidate before it can be committed below.
  "$VISUDO" -cf "$candidate"
  rm -f "$candidate"
}

check_sources() {
  validate_sudoers
  bash -n "$0"
}

install_policy() {
  if [[ -z "$DESTDIR" && "$(id -u)" != 0 ]]; then
    die "--install must run as root"
  fi

  validate_sudoers

  install -D -m 0644 \
    "$POLICY_DIR/systemd/omnigent-host.service.d/99-trusted-root.conf" \
    "$SYSTEMD_DIR/omnigent-host.service.d/99-trusted-root.conf"
  install -D -m 0644 \
    "$POLICY_DIR/systemd/omnigent-production-host.service.d/99-trusted-root.conf" \
    "$SYSTEMD_DIR/omnigent-production-host.service.d/99-trusted-root.conf"
  install -D -m 0644 \
    "$POLICY_DIR/env/omnigent-trusted-root.env" \
    "$O1_ENV_DIR/trusted-root.env"
  install -D -m 0644 \
    "$POLICY_DIR/env/omnigent-production-trusted-root.env" \
    "$O2_ENV_DIR/trusted-root.env"

  # The sudoers fragment is installed only after the candidate above passed
  # visudo. Keep the destination stable so upgrades reconcile the same rule.
  local candidate
  candidate="$(mktemp)"
  cp "$POLICY_DIR/sudoers/99-omnigent-agent-root" "$candidate"
  "$VISUDO" -cf "$candidate"
  install -D -m 0440 "$candidate" "$SUDOERS_DIR/99-omnigent-agent-root"
  rm -f "$candidate"

  # A reload makes the next explicitly managed start/restart consume the
  # policy. This script never restarts either O1 or O2.
  if [[ -z "$DESTDIR" ]]; then
    systemctl daemon-reload
  fi
}

case "${1:-}" in
  --check)
    check_sources
    ;;
  --install)
    install_policy
    ;;
  *)
    usage
    exit 2
    ;;
esac
