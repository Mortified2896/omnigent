#!/usr/bin/env bash
# Check 8 — OpenCode commits the change
#
# Symmetric to check 5.

set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/_harness_lib.sh"

: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"

require_harness_binary opencode opencode

WORKTREE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/opencode/worktree"
REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/opencode/repo"

if [ ! -d "$WORKTREE_DIR" ]; then
  BRANCH="polly/canary-8-opencode-${CANARY_RUN_ID}"
  create_fixture_repo "$REPO_DIR"
  create_worktree "$REPO_DIR" "$BRANCH" "$WORKTREE_DIR" >/dev/null
  : "${OMNIGENT_PORT:=6767}"
  : "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
  : "${CANARY_IDENTITY:=canary@omnigent.local}"
  SESSION_ID=$(run_harness_session "$OMNIGENT_AUTH_HEADER" "$CANARY_IDENTITY" \
    "$OMNIGENT_PORT" "opencode" "$WORKTREE_DIR" "$BRANCH" "implement" || true)
fi

SHA=$(read_commit_sha "$WORKTREE_DIR")
if [ -z "$SHA" ]; then
  printf 'FAIL no git.commit.outcome event in session JSON\n' >&2
  exit 1
fi
if ! git -C "$WORKTREE_DIR" cat-file -e "$SHA" 2>/dev/null; then
  printf 'FAIL git.commit.outcome SHA %s is not a valid object\n' "$SHA" >&2
  exit 1
fi
printf 'PASS\n%s\n' "$SHA"
exit 0