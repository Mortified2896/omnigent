#!/usr/bin/env bash
# Check 9 — OpenCode pushes the branch
#
# Symmetric to check 6.

set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/_harness_lib.sh"

: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"

require_harness_binary opencode opencode

WORKTREE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/opencode/worktree"
REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/opencode/repo"
REMOTE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/opencode/remote.git"
BRANCH="polly/canary-9-opencode-${CANARY_RUN_ID}"

if [ ! -d "$WORKTREE_DIR" ]; then
  create_fixture_repo "$REPO_DIR"
  create_worktree "$REPO_DIR" "$BRANCH" "$WORKTREE_DIR" >/dev/null
  : "${OMNIGENT_PORT:=6767}"
  : "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
  : "${CANARY_IDENTITY:=canary@omnigent.local}"
  SESSION_ID=$(run_harness_session     "$OMNIGENT_PORT" "opencode-native-ui" "$WORKTREE_DIR" "$BRANCH" "implement" || true)
fi

SHA=$(verify_commit_and_push "$WORKTREE_DIR" "$REMOTE_DIR" "$BRANCH") || exit 1
printf 'PASS\n%s\n' "$SHA"
exit 0