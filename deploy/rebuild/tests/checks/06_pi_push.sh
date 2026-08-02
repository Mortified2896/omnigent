#!/usr/bin/env bash
# Check 6 — Pi pushes the branch
#
# Depends on check 5 having run (and produced a commit SHA).
# Asserts the remote's `polly/canary-6-pi-<run-id>` branch tip
# equals the SHA from `git.commit.outcome`.

set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/_harness_lib.sh"

: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"

require_harness_binary pi pi

WORKTREE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/pi/worktree"
REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/pi/repo"
REMOTE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/pi/remote.git"
BRANCH="polly/canary-6-pi-${CANARY_RUN_ID}"

# If check 4 didn't run, set up the fixture here.
if [ ! -d "$WORKTREE_DIR" ]; then
  create_fixture_repo "$REPO_DIR"
  create_worktree "$REPO_DIR" "$BRANCH" "$WORKTREE_DIR" >/dev/null
  : "${OMNIGENT_PORT:=6767}"
  : "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
  : "${CANARY_IDENTITY:=canary@omnigent.local}"
  SESSION_ID=$(run_harness_session "$OMNIGENT_AUTH_HEADER" "$CANARY_IDENTITY" \
    "$OMNIGENT_PORT" "pi" "$WORKTREE_DIR" "$BRANCH" "implement" || true)
fi

SHA=$(verify_commit_and_push "$WORKTREE_DIR" "$REMOTE_DIR" "$BRANCH") || exit 1
printf 'PASS\n%s\n' "$SHA"
exit 0