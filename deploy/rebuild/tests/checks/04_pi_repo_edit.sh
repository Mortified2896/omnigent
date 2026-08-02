#!/usr/bin/env bash
# Check 4 — Pi edits a disposable repository
#
# Symmetric to check 7 (OpenCode) but with `agent_selector: "pi"`.
# Skipped if the `pi` binary is missing from PATH; once the binary
# is installed this check must report PASS, not SKIPPED.

set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/_harness_lib.sh"

: "${OMNIGENT_PORT:=6767}"
: "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
: "${CANARY_IDENTITY:=canary@omnigent.local}"
: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"

require_harness_binary pi pi

REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/pi/repo"
WORKTREE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/pi/worktree"
BRANCH="polly/canary-4-pi-${CANARY_RUN_ID}"

create_fixture_repo "$REPO_DIR"
create_worktree "$REPO_DIR" "$BRANCH" "$WORKTREE_DIR" >/dev/null

SESSION_ID=$(run_harness_session "$OMNIGENT_AUTH_HEADER" "$CANARY_IDENTITY" \
  "$OMNIGENT_PORT" "pi" "$WORKTREE_DIR" "$BRANCH" "implement" || true)

DIFF=$(git -C "$WORKTREE_DIR" diff main)
case "$DIFF" in
  *foo*) printf 'FAIL function foo still present in diff\n' >&2; exit 1 ;;
esac
case "$DIFF" in
  *bar*) printf 'PASS\n'; exit 0 ;;
esac
printf 'FAIL function bar not present in diff\n%s\n' "$DIFF" >&2
exit 1