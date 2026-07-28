#!/usr/bin/env bash
# Clean up old releases in the deploy root, retaining current and previous.
#
# Retains the ``current`` and ``previous`` releases unconditionally,
# plus any release referenced by ``deployed-sha`` or
# ``previous-deployed-sha``. Optionally retains the latest ``--keep N``
# releases (still subject to the inviolable ones above).
#
# Refuses to delete any release whose ``.venv/bin/python`` matches the
# running live process's executable — that is the live process's
# root, and deleting its containing release while the process is alive
# would be a hard production bug.
#
# Usage:
#   scripts/cleanup_releases.sh [--keep N] [--dry-run]
#
# Exit codes:
#   0  cleanup completed (or dry-run listing only).
#   1  refused to delete a release that the live process is using.
#   2  invalid arguments.

set -euo pipefail

SCRIPT_NAME="cleanup-releases"
log() { printf '[%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
fail() { printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

KEEP=3
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) KEEP="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--keep N] [--dry-run]
Removes old release dirs in \$DEPLOY_ROOT/releases, retaining the current,
previous, and recorded-deployed-sha releases plus (--keep N) most recent.
USAGE
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$KEEP" =~ ^[0-9]+$ ]] || fail "--keep must be an integer"

DEPLOY_ROOT="${DEPLOY_ROOT:-/home/hermes/workspace/deployments/omnigent}"
export OMNIGENT_DEPLOY_ROOT="$DEPLOY_ROOT"

CURRENT_LINK="$DEPLOY_ROOT/current"
PREVIOUS_LINK="$DEPLOY_ROOT/previous"
DEPLOYED_SHA_FILE="${DEPLOYED_SHA_FILE:-/home/hermes/.omnigent/deployed-sha}"
PREV_DEPLOYED_SHA_FILE="${PREV_DEPLOYED_SHA_FILE:-/home/hermes/.omnigent/previous-deployed-sha}"
RELEASES_DIR="$DEPLOY_ROOT/releases"

if [[ ! -d "$RELEASES_DIR" ]]; then
  log "no releases directory at $RELEASES_DIR; nothing to clean"
  exit 0
fi

# Build set of inviolable release SHAs.
# Keep both symlink targets and metadata files so a failed promotion cannot
# cause the previous production release to be collected.
RETENTION=()
[[ -L "$CURRENT_LINK" ]] && RETENTION+=("$(basename "$(readlink -f "$CURRENT_LINK")")")
[[ -L "$PREVIOUS_LINK" ]] && RETENTION+=("$(basename "$(readlink -f "$PREVIOUS_LINK")")")
[[ -f "$DEPLOYED_SHA_FILE" ]] && RETENTION+=("$(tr -d '[:space:]' < "$DEPLOYED_SHA_FILE")")
[[ -f "$PREV_DEPLOYED_SHA_FILE" ]] && RETENTION+=("$(tr -d '[:space:]' < "$PREV_DEPLOYED_SHA_FILE")")

UNIQUE_RETENTION=()
declare -A SEEN
for sha in "${RETENTION[@]}"; do
  if [[ -z "$sha" ]]; then continue; fi
  if [[ -z "${SEEN[$sha]:-}" ]]; then
    UNIQUE_RETENTION+=("$sha")
    SEEN[$sha]=1
  fi
done

# Discover running process; refuse to delete its release.
LIVE_EXE=""
LIVE_PID=$(systemctl show -p MainPID --value omnigent-eval-web.service 2>/dev/null || echo "")
if [[ -n "$LIVE_PID" ]] && [[ "$LIVE_PID" != "0" ]]; then
  LIVE_EXE=$(readlink "/proc/$LIVE_PID/exe" 2>/dev/null || true)
fi

# Sort release dirs by mtime (newest first).
mapfile -t RELEASE_DIRS < <(
  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@\t%p\n' \
    | sort -nr \
    | awk -F'\t' '{print $2}'
)

# Walk the sorted list, marking keepers.
declare -a KEEPERS=()
declare -a TO_DELETE=()
declare -A PINNED
for sha in "${UNIQUE_RETENTION[@]}"; do
  PINNED[$sha]=1
done

# Add the latest --keep releases to the keepers list as well, but
# only if they aren't already pinned.
for d in "${RELEASE_DIRS[@]}"; do
  sha=$(basename "$d")
  if [[ -n "${PINNED[$sha]:-}" ]]; then
    KEEPERS+=("$sha")
    continue
  fi
  if [[ ${#KEEPERS[@]} -lt ${#UNIQUE_RETENTION[@]}+$KEEP ]]; then
    KEEPERS+=("$sha")
    PINNED[$sha]=1
  else
    TO_DELETE+=("$d")
  fi
done

# Confirm none of the deletions contains the live process's executable.
for d in "${TO_DELETE[@]}"; do
  if [[ -n "$LIVE_EXE" ]] && [[ "$LIVE_EXE" == "$d"/* ]]; then
    fail "refusing to delete $d — it contains the live process executable $LIVE_EXE"
  fi
done

log "retention: current/previous/deployed + latest $KEEP releases"
log "  will keep ${#KEEPERS[@]} releases, delete ${#TO_DELETE[@]}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "would delete:"
  for d in "${TO_DELETE[@]}"; do echo "  $d"; done
  exit 0
fi

for d in "${TO_DELETE[@]}"; do
  log "removing $d"
  rm -rf "$d"
done
log "cleanup complete"
