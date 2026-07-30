#!/usr/bin/env bash
# Shared helper for the canonical live-deployed-SHA marker.
#
# Sourced by promote_release.sh and rollback_release.sh so both
# scripts read and write the same file. The external updater
# daemon reads the same file via OMNIGENT_UPDATER_LIVE_SHA_FILE.
#
# Path resolution:
#   OMNIGENT_DEPLOYED_SHA_FILE   override (used by tests + updater)
#   OMNIGENT_DEPLOYED_SHA_DIR    override for the shared directory
#   /var/lib/omnigent/shared     shared default (writable by both
#                                hermes and the omnigent-updater user)
#   $HOME/.omnigent              legacy fallback (still honored when
#                                the shared marker is absent, so the
#                                transition to the external updater
#                                does not strand an existing install)

set -euo pipefail

# shellcheck disable=SC2155  # we want the assignment+export visible separately
if [[ -n "${OMNIGENT_DEPLOYED_SHA_FILE:-}" ]]; then
    DEPLOYED_SHA_FILE="$OMNIGENT_DEPLOYED_SHA_FILE"
elif [[ -n "${OMNIGENT_DEPLOYED_SHA_DIR:-}" ]]; then
    DEPLOYED_SHA_FILE="$OMNIGENT_DEPLOYED_SHA_DIR/deployed-sha"
elif [[ -w /var/lib/omnigent/shared ]] && [[ -d /var/lib/omnigent/shared ]]; then
    DEPLOYED_SHA_FILE="/var/lib/omnigent/shared/deployed-sha"
elif [[ -n "${HOME:-}" ]]; then
    DEPLOYED_SHA_FILE="$HOME/.omnigent/deployed-sha"
else
    DEPLOYED_SHA_FILE="/var/lib/omnigent/shared/deployed-sha"
fi
export DEPLOYED_SHA_FILE

if [[ -n "${OMNIGENT_PREV_DEPLOYED_SHA_FILE:-}" ]]; then
    PREV_DEPLOYED_SHA_FILE="$OMNIGENT_PREV_DEPLOYED_SHA_FILE"
elif [[ -n "${OMNIGENT_DEPLOYED_SHA_DIR:-}" ]]; then
    PREV_DEPLOYED_SHA_FILE="$OMNIGENT_DEPLOYED_SHA_DIR/previous-deployed-sha"
elif [[ -w /var/lib/omnigent/shared ]] && [[ -d /var/lib/omnigent/shared ]]; then
    PREV_DEPLOYED_SHA_FILE="/var/lib/omnigent/shared/previous-deployed-sha"
elif [[ -n "${HOME:-}" ]]; then
    PREV_DEPLOYED_SHA_FILE="$HOME/.omnigent/previous-deployed-sha"
else
    PREV_DEPLOYED_SHA_FILE="/var/lib/omnigent/shared/previous-deployed-sha"
fi
export PREV_DEPLOYED_SHA_FILE

_deployed_sha_mkdir() {
    mkdir -p "$(dirname "$DEPLOYED_SHA_FILE")"
    mkdir -p "$(dirname "$PREV_DEPLOYED_SHA_FILE")"
}

_deployed_sha_read() {
    if [[ -r "$DEPLOYED_SHA_FILE" ]]; then
        cat "$DEPLOYED_SHA_FILE"
    fi
}

# Atomic write: writes to <file>.tmp then mv -T moves into place so
# a parallel reader never sees a half-written SHA.
_deployed_sha_write() {
    local target="$1"
    local sha="$2"
    _deployed_sha_mkdir
    local tmp="${target}.tmp"
    printf '%s\n' "$sha" > "$tmp"
    mv -T "$tmp" "$target"
}

_deployed_sha_write_current() { _deployed_sha_write "$DEPLOYED_SHA_FILE" "$1"; }
_deployed_sha_write_previous() { _deployed_sha_write "$PREV_DEPLOYED_SHA_FILE" "$1"; }
