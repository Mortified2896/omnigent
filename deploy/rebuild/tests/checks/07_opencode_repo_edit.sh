#!/usr/bin/env bash
# Check 7 — OpenCode edits a disposable repository
#
# Symmetric to check 4 (Pi) but with `agent_name: "opencode-native-ui"`
# (resolved to the wheel's agent_id before the POST).
# Skipped if the `opencode` binary is missing from PATH; once the
# binary is installed this check must report PASS, not SKIPPED.

set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/_harness_lib.sh"

: "${OMNIGENT_PORT:=6767}"
: "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
: "${CANARY_IDENTITY:=canary@omnigent.local}"
: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"

require_harness_binary opencode opencode

REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/opencode/repo"
WORKTREE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/opencode/worktree"
BRANCH="polly/canary-7-opencode-${CANARY_RUN_ID}"

create_fixture_repo "$REPO_DIR"
create_worktree "$REPO_DIR" "$BRANCH" "$WORKTREE_DIR" >/dev/null

SESSION_ID=$(run_harness_session   "$OMNIGENT_PORT" "opencode-native-ui" "$WORKTREE_DIR" "$BRANCH" "implement") || {
  printf 'FAIL run_harness_session for opencode-native-ui on port %s (agent/host/session prerequisite failed)\n' "$OMNIGENT_PORT" >&2
  exit 1
}

# Assert the edit in the worktree itself. A rename shows up in
# `git diff main` as `-def foo():` / `+def bar():`, so grepping the
# diff for `foo` can never pass — check the file content instead.
if ! grep -q 'def bar' "$WORKTREE_DIR/module.py"; then
  printf 'FAIL function bar not present in %s\n' "$WORKTREE_DIR/module.py" >&2
  exit 1
fi
if grep -q 'def foo' "$WORKTREE_DIR/module.py"; then
  printf 'FAIL function foo still present in %s\n' "$WORKTREE_DIR/module.py" >&2
  exit 1
fi

printf 'PASS\n'
exit 0
