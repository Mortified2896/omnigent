#!/usr/bin/env bash
# Validate the historical Control Room broad-privilege bundle.
#
# Installation is intentionally disabled. The retained files are non-operative
# historical evidence and must not be restored to a host.

set -euo pipefail

POLICY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
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
  printf 'usage: %s --check\n' "$0" >&2
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
  # Validate the retained historical fragment without installing it.
  "$VISUDO" -cf "$candidate"
  rm -f "$candidate"
}

check_sources() {
  validate_sudoers
  bash -n "$0"
}

case "${1:-}" in
  --check)
    check_sources
    ;;
  --install)
    die "installation disabled: this bundle is historical and must not be restored"
    ;;
  *)
    usage
    exit 2
    ;;
esac
