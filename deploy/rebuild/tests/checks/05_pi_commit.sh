#!/usr/bin/env bash
# Check 5 — Pi commits the change
#
# Depends on check 4 having run (or runs the session standalone
# when the worktree does not exist yet).

set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/_harness_lib.sh"

: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"

require_harness_binary pi pi

WORKTREE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/pi/worktree"
REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/pi/repo"

# If check 3 didn't run (the user ran checks individually), set
# up the fixture here.
if [ ! -d "$WORKTREE_DIR" ]; then
  REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/pi/repo"
  BRANCH="polly/canary-5-pi-${CANARY_RUN_ID}"
  create_fixture_repo "$REPO_DIR"
  create_worktree "$REPO_DIR" "$BRANCH" "$WORKTREE_DIR" >/dev/null
  : "${OMNIGENT_PORT:=6767}"
  : "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
  : "${CANARY_IDENTITY:=canary@omnigent.local}"
  SESSION_ID=$(run_harness_session     "$OMNIGENT_PORT" "pi-native-ui" "$WORKTREE_DIR" "$BRANCH" "implement") || {
    printf 'FAIL run_harness_session for pi-native-ui on port %s (agent/host/session prerequisite failed)\n' "$OMNIGENT_PORT" >&2
    exit 1
  }
fi

SHA=$(read_commit_sha "$WORKTREE_DIR")
if [ -z "$SHA" ]; then
  printf 'FAIL no harness commit in worktree %s\n' "$WORKTREE_DIR" >&2
  exit 1
fi

# Confirm the commit actually exists in the worktree.
if ! git -C "$WORKTREE_DIR" cat-file -e "$SHA" 2>/dev/null; then
  printf 'FAIL harness commit SHA %s is not a valid object in %s\n' "$SHA" "$WORKTREE_DIR" >&2
  exit 1
fi

printf 'PASS\n%s\n' "$SHA"
exit 0
