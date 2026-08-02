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

# Canary auth handling:
#
# The canary wheel runs on 127.0.0.1 in single-user local mode
# (OMNIGENT_LOCAL_SINGLE_USER=1 is auto-set when the server binds
# loopback with the default header auth source). In that mode:
#   * /v1/hosts unauthenticated returns the local-owned hosts.
#   * POST /v1/sessions unauthenticated resolves to RESERVED_USER_LOCAL
#     and binds the session to whatever host_id was supplied.
#   * An X-Forwarded-Email header from the canary script makes the
#     request resolve to that email instead of "local"; the temp
#     host is owned by "local", so the bind fails with
#     "not your host" / 403.
#
# Helpers that talk to the canary wheel therefore DO NOT send any
# auth header by default. An operator running these checks against
# a production-shape wheel can still opt in by setting
# CANARY_AUTH_HEADER / CANARY_AUTH_IDENTITY before invoking.
: "${CANARY_AUTH_HEADER:=}"
: "${CANARY_AUTH_IDENTITY:=}"

# Never let git prompt interactively for credentials while the
# canary runs. Fixture remotes are local bare paths, so no
# credential helper is needed; if an accidental http(s) remote is
# ever pushed to, git must fail fast instead of hanging on a
# username prompt (or invoking the operator's github helper).
export GIT_TERMINAL_PROMPT=0

# Resolve an agent NAME to its database id via GET /v1/agents.
# Usage: resolve_agent_id <port> <name>
#
# Uses CANARY_AUTH_HEADER / CANARY_AUTH_IDENTITY env vars when set;
# the canary local mode leaves them unset so the request is
# unauthenticated (which the single-user wheel treats as the
# reserved "local" user — same identity as the temporary host).
resolve_agent_id() {
  local port="$1" name="$2"
  local extra=()
  if [ -n "$CANARY_AUTH_HEADER" ]; then
    extra=(-H "${CANARY_AUTH_HEADER}: ${CANARY_AUTH_IDENTITY}")
  fi
  curl -fsS --max-time 10 \
    "${extra[@]}" \
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
#
# Fail-fast on every step: any `git` failure exits 1 so a dependent
# check never inherits a half-built fixture. Returns the bare remote
# dir on stdout (callers don't need it; logged for debug).
create_fixture_repo() {
  local repo_dir="$1"
  rm -rf "$repo_dir"
  mkdir -p "$repo_dir" \
    || { printf 'FAIL create_fixture_repo: mkdir %s failed\n' "$repo_dir" >&2; exit 1; }
  git -C "$repo_dir" init --quiet --initial-branch=main \
    || { printf 'FAIL create_fixture_repo: git init at %s failed\n' "$repo_dir" >&2; exit 1; }
  git -C "$repo_dir" config user.email "canary@omnigent.local" \
    || { printf 'FAIL create_fixture_repo: git config user.email at %s failed\n' "$repo_dir" >&2; exit 1; }
  git -C "$repo_dir" config user.name "Canary Bot" \
    || { printf 'FAIL create_fixture_repo: git config user.name at %s failed\n' "$repo_dir" >&2; exit 1; }
  # Disable the operator's inherited github credential helper for this
  # repo so the canary process (which inherits $HOME from the runner
  # shell) never tries to talk to github.com from inside the
  # fixture. The fixture remote is a local file path; no network
  # auth is needed.
  git -C "$repo_dir" config credential.helper "" \
    || { printf 'FAIL create_fixture_repo: git config credential.helper at %s failed\n' "$repo_dir" >&2; exit 1; }
  printf 'def foo():\n    return 1\n' >"$repo_dir/module.py"
  git -C "$repo_dir" add module.py \
    || { printf 'FAIL create_fixture_repo: git add at %s failed\n' "$repo_dir" >&2; exit 1; }
  git -C "$repo_dir" commit --quiet -m "initial" \
    || { printf 'FAIL create_fixture_repo: initial commit at %s failed\n' "$repo_dir" >&2; exit 1; }
  # Bare remote.
  local remote_dir="${repo_dir%/*}/remote.git"
  rm -rf "$remote_dir"
  git init --quiet --bare "$remote_dir" \
    || { printf 'FAIL create_fixture_repo: bare remote init at %s failed\n' "$remote_dir" >&2; exit 1; }
  git -C "$repo_dir" remote add origin "$remote_dir" \
    || { printf 'FAIL create_fixture_repo: remote add origin at %s failed\n' "$repo_dir" >&2; exit 1; }
  git -C "$repo_dir" push --quiet --set-upstream origin main \
    || { printf 'FAIL create_fixture_repo: initial push to %s failed\n' "$remote_dir" >&2; exit 1; }
  printf '%s\n' "$remote_dir"
}

# Create a fresh disposable worktree under
#   /tmp/canary-fixtures/<run-id>/<harness>/worktree
# bound to a NEW branch. Returns the worktree path on stdout.
#
# Fail-fast: any git error exits 1 so a downstream check never
# inherits a broken or missing worktree. Stale .git/worktrees
# entries / index.lock files left behind by a prior crashed run
# are cleared up-front so `git worktree add` doesn't refuse on a
# leftover lock or stale metadata.
create_worktree() {
  local repo_dir="$1"
  local branch="$2"
  local worktree_dir="$3"
  rm -rf "$worktree_dir"
  # Stale index.lock in the parent repo blocks worktree add. The
  # canary never holds a real git index lock — it's a leftover
  # from a crashed previous run. Remove defensively.
  rm -f "$repo_dir/.git/index.lock"
  git -C "$repo_dir" worktree prune \
    || true
  git -C "$repo_dir" worktree add -b "$branch" "$worktree_dir" main >/dev/null \
    || { printf 'FAIL create_worktree: git worktree add at %s failed\n' "$worktree_dir" >&2; exit 1; }
  # Sanity check the worktree.
  [ -d "$worktree_dir/.git" ] || [ -f "$worktree_dir/.git" ] \
    || { printf 'FAIL create_worktree: %s has no .git after add\n' "$worktree_dir" >&2; exit 1; }
  printf '%s\n' "$worktree_dir"
}

# Resolve an online host_id from GET /v1/hosts. Usage:
#   resolve_host_id <port>
#
# Auth is intentionally omitted: in the canary's local mode the
# server has no auth_provider, so /v1/hosts reads the reserved
# "local" user. With auth enabled and the canary's identity
# (canary@omnigent.local) attached, the same call returns []
# because the temporary host is owned by "local", not by the
# canary user. Polling the unauthenticated route is the correct
# local-mode behavior.
resolve_host_id() {
  local port="$1"
  curl -fsS --max-time 10 \
    "http://127.0.0.1:${port}/v1/hosts" \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
hosts = d.get('hosts') or d.get('data') or []
for h in hosts:
    if h.get('status') == 'online' and h.get('host_id'):
        print(h['host_id'])
        break
" 2>/dev/null
}

# Launch a session via POST /v1/sessions and wait for it to
# complete. Usage:
#   run_harness_session <port> <agent_name>
#                       <worktree_path> <branch> <purpose>
# Prints the session_id on stdout.
# Side-effects: writes the SSE stream to $WORKTREE_DIR/.sse.log
# and the session create JSON to $WORKTREE_DIR/.session.json as
# evidence for follow-up checks.
#
# Wire format: upstream v0.7 binds a session by `agent_id` (a
# durable db id resolved via GET /v1/agents), not by name. The
# helper resolves <agent_name> -> <agent_id> and posts that. The
# session also needs a `host_id` to actually dispatch to a host —
# the server uses `host_id` to generate a binding token and send
# the host a `host.launch_runner` frame over its tunnel. Without
# it, the session is unbound and never gets a runner, so it sits
# in `idle` indefinitely.
#
# Auth: when CANARY_AUTH_HEADER / CANARY_AUTH_IDENTITY are set
# (production-shape wheel behind a proxy), the helper sends the
# configured header. In the canary's local mode both are empty
# and the request goes unauthenticated — the loopback single-user
# wheel resolves the absent header to RESERVED_USER_LOCAL, which
# owns the temporary host.
run_harness_session() {
  local port="$1" agent_name="$2" worktree="$3" branch="$4" purpose="$5"
  local session_url="http://127.0.0.1:${port}/v1/sessions"
  local auth_args=()
  if [ -n "$CANARY_AUTH_HEADER" ]; then
    auth_args=(-H "${CANARY_AUTH_HEADER}: ${CANARY_AUTH_IDENTITY}")
  fi
  local agent_id host_id
  agent_id=$(resolve_agent_id "$port" "$agent_name")
  if [ -z "$agent_id" ]; then
    printf 'FAIL could not resolve agent_id for agent_name=%s on port %s\n' "$agent_name" "$port" >&2
    return 1
  fi
  host_id=$(resolve_host_id "$port")
  if [ -z "$host_id" ]; then
    printf 'FAIL could not resolve an online host_id on port %s\n' "$port" >&2
    return 1
  fi
  local body
  # Build the JSON in a python heredoc to avoid bash
  # command-substitution eating the backticks in the prompt text.
  # An earlier version of this helper used `body=$(cat <<JSON ...)`
  # with literal backticks around the git commands, and bash
  # executed those backticks at heredoc-expand time — the captured
  # session payload ended up containing `git status` output
  # ("On branch rebuild/upstream-0.7...") instead of the intended
  # git instructions. Single-quote the python heredoc to keep the
  # payload byte-exact.
  #
  # The argv args must sit on the SAME line as the heredoc redirect
  # (`python3 - "$arg" ... <<'PYBODY'`): words after the PYBODY
  # terminator line would be parsed as a separate command, not as
  # python's argv, and python would raise IndexError on sys.argv[1].
  body=$(python3 - "$agent_id" "$host_id" "$purpose" "$worktree" "canary-${agent_name}-${branch}" <<'PYBODY'
import json, sys
print(json.dumps({
    "agent_id": sys.argv[1],
    "host_id": sys.argv[2],
    "purpose": sys.argv[3],
    "workspace": sys.argv[4],
    "title": sys.argv[5],
    "initial_items": [{
        "type": "message",
        "data": {
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": (
                    "rename the function foo to bar in module.py. "
                    "Then run `git commit -am 'rename foo to bar'` "
                    "and `git push -u origin HEAD`. "
                    "Report what you did."
                ),
            }],
        },
    }],
}, indent=None))
PYBODY
)
  local session_json
  session_json=$(curl -fsS --max-time 30 \
    -H "Content-Type: application/json" \
    "${auth_args[@]}" \
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
  # Wake the runner. SessionCreateRequest's ``initial_items`` only
  # seeds the conversation row — the runner does not auto-process
  # them on bind. The runner's POST /v1/sessions/{id}/events is
  # the only thing that drives a turn. Re-POST the user message
  # here so the runner picks it up and dispatches the harness turn.
  local wake_body
  wake_body=$(python3 - <<'PYBODY'
import json
print(json.dumps({
    "type": "message",
    "data": {
        "role": "user",
        "content": [{
            "type": "input_text",
            "text": (
                "rename the function foo to bar in module.py. "
                "Then run `git commit -am 'rename foo to bar'` "
                "and `git push -u origin HEAD`. "
                "Report what you did."
            ),
        }],
    },
}))
PYBODY
)
  curl -fsS --max-time 30 \
    -H "Content-Type: application/json" \
    "${auth_args[@]}" \
    -X POST \
    -d "$wake_body" \
    "${session_url}/${session_id}/events" >/dev/null 2>&1 || {
      printf 'FAIL POST /v1/sessions/%s/events failed\n' "$session_id" >&2
      return 1
    }
  # Wait for the harness to complete the work. For the native-TUI
  # harnesses (pi, opencode) the runner dispatches the turn and the
  # session returns to `idle` immediately — the TUI then performs
  # the edit/commit/push asynchronously, so session status does not
  # track progress. Poll the worktree instead: the work is done when
  # the worktree HEAD moves past the fixture's `main`. `failed`
  # status is a hard error; no work within 90 s means the runner
  # never picked up the wake (fail fast rather than waiting out the
  # full deadline).
  # The default session deadline is 240 s. CANARY_SESSION_DEADLINE_S
  # can override it (used by the canary-run orchestrator to keep
  # the total canary wall-time bounded).
  local deadline=$(($SECONDS + ${CANARY_SESSION_DEADLINE_S:-240}))
  local progress_deadline=$(($SECONDS + 90))
  local main_sha
  main_sha=$(git -C "$worktree" rev-parse main 2>/dev/null || true)
  while [ "$SECONDS" -lt "$deadline" ]; do
    local status head
    status=$(curl -fsS --max-time 5 \
      "${auth_args[@]}" \
      "http://127.0.0.1:${port}/v1/sessions/${session_id}" \
      | python3 -c "import sys, json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)
    if [ "$status" = failed ]; then
      printf 'FAIL session %s reported status failed\n' "$session_id" >&2
      printf '%s\n' "$session_id"
      return 1
    fi
    head=$(git -C "$worktree" rev-parse HEAD 2>/dev/null || true)
    if [ -n "$head" ] && [ -n "$main_sha" ] && [ "$head" != "$main_sha" ]; then
      printf '%s\n' "$session_id"
      return 0
    fi
    if [ "$SECONDS" -ge "$progress_deadline" ]; then
      printf 'FAIL session %s made no progress in worktree %s (runner missing?)\n' "$session_id" "$worktree" >&2
      printf '%s\n' "$session_id"
      return 1
    fi
    sleep 2
  done
  printf '%s\n' "$session_id"
  return 1
}

# Read the commit the harness made in the worktree. The upstream
# v0.7 session event surface has no git.commit.outcome event, so the
# SHA is read from the worktree HEAD itself. Usage:
#   read_commit_sha <worktree_path>
# Prints the commit SHA on stdout, or empty. Returns 1 if the
# worktree has no commit beyond the fixture's main (the harness did
# not commit), so downstream checks fail rather than false-pass.
read_commit_sha() {
  local worktree="$1"
  local head main_sha
  head=$(git -C "$worktree" rev-parse HEAD 2>/dev/null) || return 1
  main_sha=$(git -C "$worktree" rev-parse main 2>/dev/null || true)
  if [ -n "$main_sha" ] && [ "$head" = "$main_sha" ]; then
    return 1
  fi
  printf '%s\n' "$head"
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
  [ -n "$sha" ] || { printf 'FAIL no harness commit found in worktree\n' >&2; return 1; }
  # The TUI pushes a moment after committing; poll briefly so the
  # check does not race the push.
  local remote_sha=""
  local deadline=$(($SECONDS + 30))
  while [ "$SECONDS" -lt "$deadline" ]; do
    remote_sha=$(git -C "$remote_dir" rev-parse "refs/heads/${branch}" 2>/dev/null || true)
    if [ "$remote_sha" = "$sha" ]; then
      printf '%s\n' "$sha"
      return 0
    fi
    sleep 2
  done
  printf 'FAIL remote branch %s has SHA %s (expected %s)\n' "$branch" "$remote_sha" "$sha" >&2
  return 1
}
