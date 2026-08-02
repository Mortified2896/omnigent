#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Shared helpers for the per-harness Pi / OpenCode checks
# (04_pi_repo_edit, 05_pi_commit, 06_pi_push, 07_opencode_repo_edit,
#  08_opencode_commit, 09_opencode_push).
# ─────────────────────────────────────────────────────────────────────
#
# Sourced (not executed) by each per-harness check.

# Reject if the harness binary is missing on PATH (canary runner
# records SKIPPED for that case; the helper exists so each check
# implements the same logic).
require_harness_binary() {
  local harness_name="$1"
  local binary="$2"
  if ! command -v "$binary" >/dev/null 2>&1; then
    printf 'SKIPPED (harness binary missing: %s)\n' "$binary" >&2
    exit 0
  fi
}

# Resolve an agent NAME to its database id via GET /v1/agents.
# Usage: resolve_agent_id <port> <auth_header> <identity> <agent_name>
# Prints the id on stdout, or empty.
resolve_agent_id() {
  local port="$1" auth_header="$2" identity="$3" name="$4"
  curl -fsS --max-time 10 \
    -H "${auth_header}: ${identity}" \
    "http://127.0.0.1:${port}/v1/agents" \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
for a in d.get('data', []):
    if a.get('name') == '${name}':
        print(a.get('id', ''))
        break
"
}

# Create a fresh disposable git fixture repo. Usage:
#   create_fixture_repo /tmp/canary-fixtures/<run-id>/<harness>/repo
# Writes a single file `module.py` containing `def foo(): return 1`
# and commits it on `main`.
create_fixture_repo() {
  local repo_dir="$1"
  rm -rf "$repo_dir"
  mkdir -p "$repo_dir"
  git -C "$repo_dir" init --quiet --initial-branch=main
  git -C "$repo_dir" config user.email "canary@omnigent.local"
  git -C "$repo_dir" config user.name "Canary Bot"
  printf 'def foo():\n    return 1\n' >"$repo_dir/module.py"
  git -C "$repo_dir" add module.py
  git -C "$repo_dir" commit --quiet -m "initial"
  # Bare remote.
  local remote_dir="${repo_dir%/*}/remote.git"
  rm -rf "$remote_dir"
  git init --quiet --bare "$remote_dir"
  git -C "$repo_dir" remote add origin "$remote_dir"
  git -C "$repo_dir" push --quiet --set-upstream origin main
}

# Create a fresh disposable worktree under
#   /tmp/canary-fixtures/<run-id>/<harness>/worktree
# bound to a NEW branch. Returns the worktree path on stdout.
create_worktree() {
  local repo_dir="$1"
  local branch="$2"
  local worktree_dir="$3"
  rm -rf "$worktree_dir"
  git -C "$repo_dir" worktree add -b "$branch" "$worktree_dir" main >/dev/null
  printf '%s\n' "$worktree_dir"
}

# Launch a session via POST /v1/sessions and wait for it to
# complete. Usage:
#   run_harness_session <auth-header-name> <identity>
#                       <port> <agent_name>
#                       <worktree_path> <branch> <purpose>
# Prints the session_id on stdout.
# Side-effects: writes the SSE stream to $WORKTREE_DIR/.sse.log
# and the session JSON to $WORKTREE_DIR/.session.json (so the
# follow-up commit/push checks can read the git.commit.outcome
# event without re-running).
#
# Wire format: upstream v0.7 binds a session by `agent_id` (a
# durable db id resolved via GET /v1/agents), not by name. The
# helper resolves <agent_name> → <agent_id> and posts that.
run_harness_session() {
  local auth_header="$1" identity="$2" port="$3" agent_name="$4" \
        worktree="$5" branch="$6" purpose="$7"
  local session_url="http://127.0.0.1:${port}/v1/sessions"
  local agent_id
  agent_id=$(resolve_agent_id "$port" "$auth_header" "$identity" "$agent_name")
  if [ -z "$agent_id" ]; then
    printf 'FAIL could not resolve agent_id for agent_name=%s on port %s\n' "$agent_name" "$port" >&2
    return 1
  fi
  local body
  body=$(cat <<JSON
{
  "agent_id": "${agent_id}",
  "purpose": "${purpose}",
  "workspace": "${worktree}",
  "prompt": "rename the function foo to bar. Run pytest, lint, and typecheck on what you changed. Push your branch when green. Open a PR with gh pr create if a remote is configured.",
  "title": "canary-${agent_name}-${branch}"
}
JSON
)
  local session_json
  session_json=$(curl -fsS --max-time 30 \
    -H "Content-Type: application/json" \
    -H "${auth_header}: ${identity}" \
    -X POST -d "$body" "$session_url" 2>/dev/null) || {
      printf 'FAIL POST /v1/sessions failed; body: %s\n' "$body" >&2
      return 1
    }
  printf '%s' "$session_json" >"$worktree/.session.json"
  local session_id
  session_id=$(printf '%s' "$session_json" | python3 -c "import sys, json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null) || true
  if [ -z "$session_id" ]; then
    printf 'FAIL session create returned no id; body: %s\n' "$session_json" >&2
    return 1
  fi
  # Wait for the session to reach status 'finished' or 'failed'.
  local deadline=$((SECONDS + 240))
  while [ "$SECONDS" -lt "$deadline" ]; do
    local status
    status=$(curl -fsS --max-time 5 \
      -H "${auth_header}: ${identity}" \
      "http://127.0.0.1:${port}/v1/sessions/${session_id}" \
      | python3 -c "import sys, json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)
    case "$status" in
      finished) printf '%s\n' "$session_id"; return 0 ;;
      failed)   printf '%s\n' "$session_id"; return 1 ;;
    esac
    sleep 2
  done
  printf '%s\n' "$session_id"
  return 1
}

# Read the most recent git.commit.outcome event from a session's
# JSON dump. Usage:
#   read_commit_sha <worktree_path>
# Prints the commit SHA on stdout, or empty.
read_commit_sha() {
  local worktree="$1"
  local session_json="$worktree/.session.json"
  if [ ! -f "$session_json" ]; then
    return 1
  fi
  python3 - <<PY
import json
with open("$session_json") as f:
    s = json.load(f)
events = s.get("events", [])
sha = ""
for ev in events:
    if ev.get("type") == "git.commit.outcome":
        sha = ev.get("payload", {}).get("commit_sha", "")
print(sha)
PY
}

# Verify the commit + push.
# Usage:
#   verify_commit_and_push <worktree_path> <remote_bare_dir> <branch>
verify_commit_and_push() {
  local worktree="$1"
  local remote_dir="$2"
  local branch="$3"
  local sha
  sha=$(read_commit_sha "$worktree")
  [ -n "$sha" ] || { printf 'FAIL no git.commit.outcome event in session JSON\n' >&2; return 1; }
  local remote_sha
  remote_sha=$(git -C "$remote_dir" rev-parse "refs/heads/${branch}" 2>/dev/null || true)
  if [ "$remote_sha" != "$sha" ]; then
    printf 'FAIL remote branch %s has SHA %s (expected %s)\n' "$branch" "$remote_sha" "$sha" >&2
    return 1
  fi
  printf '%s\n' "$sha"
  return 0
}
