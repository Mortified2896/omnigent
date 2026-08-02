#!/usr/bin/env bash
# Check 12 — Parallel workers remain isolated in separate worktrees
#
# Runs two parallel Pi sessions in parallel against the same
# fixture repo, verifies each gets its own worktree, distinct
# branches, distinct commits, and no cross-session filesystem
# contamination.

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

REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/parallel/repo"
REMOTE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/parallel/remote.git"
WT_A="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/parallel/wt-a"
WT_B="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/parallel/wt-b"

# Fixtures.
create_fixture_repo "$REPO_DIR"
# create_fixture_repo already pushed main to the bare remote.
create_worktree "$REPO_DIR" "polly/canary-12-pi-a-${CANARY_RUN_ID}" "$WT_A" >/dev/null
create_worktree "$REPO_DIR" "polly/canary-12-pi-b-${CANARY_RUN_ID}" "$WT_B" >/dev/null

# Snapshot each worktree's filesystem before the run.
WT_A_HASH_BEFORE=$(find "$WT_A" -type f -exec sha256sum {} \; | sort | sha256sum)
WT_B_HASH_BEFORE=$(find "$WT_B" -type f -exec sha256sum {} \; | sort | sha256sum)

# Launch the two sessions in parallel.
run_harness_session   "$OMNIGENT_PORT" "pi-native-ui" "$WT_A" "polly/canary-12-pi-a-${CANARY_RUN_ID}" "implement" >/dev/null &
PID_A=$!
run_harness_session   "$OMNIGENT_PORT" "pi-native-ui" "$WT_B" "polly/canary-12-pi-b-${CANARY_RUN_ID}" "implement" >/dev/null &
PID_B=$!
wait "$PID_A" || true
wait "$PID_B" || true

# The two commits must be byte-distinct.
SHA_A=$(read_commit_sha "$WT_A")
SHA_B=$(read_commit_sha "$WT_B")
[ -n "$SHA_A" ] || { printf 'FAIL no git.commit.outcome in session A\n' >&2; exit 1; }
[ -n "$SHA_B" ] || { printf 'FAIL no git.commit.outcome in session B\n' >&2; exit 1; }
[ "$SHA_A" != "$SHA_B" ] || { printf 'FAIL parallel sessions produced identical SHA %s\n' "$SHA_A" >&2; exit 1; }

# The two commits must exist on the remote.
REMOTE_A=$(git -C "$REMOTE_DIR" rev-parse "polly/canary-12-pi-a-${CANARY_RUN_ID}" 2>/dev/null || true)
REMOTE_B=$(git -C "$REMOTE_DIR" rev-parse "polly/canary-12-pi-b-${CANARY_RUN_ID}" 2>/dev/null || true)
[ "$REMOTE_A" = "$SHA_A" ] || { printf 'FAIL remote polly/canary-12-pi-a-%s is %s, expected %s\n' "$CANARY_RUN_ID" "$REMOTE_A" "$SHA_A" >&2; exit 1; }
[ "$REMOTE_B" = "$SHA_B" ] || { printf 'FAIL remote polly/canary-12-pi-b-%s is %s, expected %s\n' "$CANARY_RUN_ID" "$REMOTE_B" "$SHA_B" >&2; exit 1; }

# The OTHER session's worktree must be unchanged.
WT_A_HASH_AFTER=$(find "$WT_A" -type f -exec sha256sum {} \; | sort | sha256sum)
WT_B_HASH_AFTER=$(find "$WT_B" -type f -exec sha256sum {} \; | sort | sha256sum)
[ "$WT_A_HASH_BEFORE" = "$WT_A_HASH_AFTER" ] || { printf 'FAIL session B wrote into session A's worktree\n' >&2; exit 1; }
[ "$WT_B_HASH_BEFORE" = "$WT_B_HASH_AFTER" ] || { printf 'FAIL session A wrote into session B's worktree\n' >&2; exit 1; }

printf 'PASS\n%s\n%s\n' "$SHA_A" "$SHA_B"
exit 0