#!/usr/bin/env bash
# Check 10 — Verity orchestrates delegation correctly
#
# Boots the wheel with the three agent bundles (verity, pi,
# opencode) registered, posts a session with agent_selector:
# "verity", prompts for a Pi delegation, and verifies the
# parent's stream carries the full delegation lifecycle.
#
# Requires: python3, curl, jq (or python3's json module).

set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"

: "${OMNIGENT_PORT:=6767}"
: "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
: "${CANARY_IDENTITY:=canary@omnigent.local}"
: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"

REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/verity/repo"
WORKTREE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/verity/worktree"

# Fixture repo + worktree.
rm -rf "$REPO_DIR" "$WORKTREE_DIR"
mkdir -p "$REPO_DIR"
git -C "$REPO_DIR" init --quiet --initial-branch=main
git -C "$REPO_DIR" config user.email "canary@omnigent.local"
git -C "$REPO_DIR" config user.name "Canary Bot"
printf 'def foo():\n    return 1\n' >"$REPO_DIR/module.py"
git -C "$REPO_DIR" add module.py
git -C "$REPO_DIR" commit --quiet -m "initial"
git -C "$REPO_DIR" worktree add -b "polly/acceptance-10-verity" "$WORKTREE_DIR" main >/dev/null

# Post the session.
SESSIONS_URL="http://127.0.0.1:${OMNIGENT_PORT}/v1/sessions"
BODY=$(cat <<JSON
{
  "agent_selector": "verity",
  "workspace": "${WORKTREE_DIR}",
  "prompt": "rename foo to bar in module.py using a Pi sub-agent. Push the branch when green.",
  "title": "acceptance-10-verity"
}
JSON
)

SESSION_JSON=$(curl -fsS --max-time 30 \
  -H "Content-Type: application/json" \
  -H "${OMNIGENT_AUTH_HEADER}: ${CANARY_IDENTITY}" \
  -X POST -d "$BODY" "$SESSIONS_URL")
SESSION_ID=$(printf '%s' "$SESSION_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")

# Wait up to 240 s for the parent session to finish.
DEADLINE=$((SECONDS + 240))
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  STATUS=$(curl -fsS --max-time 5 \
    -H "${OMNIGENT_AUTH_HEADER}: ${CANARY_IDENTITY}" \
    "http://127.0.0.1:${OMNIGENT_PORT}/v1/sessions/${SESSION_ID}" \
    | python3 -c "import sys, json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)
  case "$STATUS" in
    finished) break ;;
    failed)   printf 'FAIL verity session failed\n' >&2; exit 1 ;;
  esac
  sleep 2
done

# Read the full session JSON.
FULL=$(curl -fsS --max-time 10 \
  -H "${OMNIGENT_AUTH_HEADER}: ${CANARY_IDENTITY}" \
  "http://127.0.0.1:${OMNIGENT_PORT}/v1/sessions/${SESSION_ID}")

# Verify the lifecycle events.
python3 - "$FULL" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
events = data.get("events", [])

def has_event(predicate):
    for ev in events:
        if predicate(ev):
            return True
    return False

if not has_event(lambda ev: ev.get("type") == "session.created" and ev.get("payload", {}).get("agent_selector") == "verity"):
    print("FAIL no session.created with agent_selector=verity"); sys.exit(1)
if not has_event(lambda ev: "sys_session_send" in (ev.get("type") or "") and ev.get("payload", {}).get("purpose") == "implement"):
    print("FAIL no sys_session_send with purpose=implement"); sys.exit(1)
if not has_event(lambda ev: ev.get("type") == "session.harness" and ev.get("payload", {}).get("harness") == "pi-native"):
    print("FAIL no session.harness with harness=pi-native"); sys.exit(1)
if not has_event(lambda ev: ev.get("type") == "session.child_session.updated"):
    print("FAIL no session.child_session.updated event"); sys.exit(1)

# Inbox check: parent's sys_read_inbox must contain at least one
# sub_agent item.
inbox = data.get("inbox", [])
if not any(it.get("type") == "sub_agent" for it in inbox):
    print("FAIL parent's inbox has no sub_agent item"); sys.exit(1)

print("PASS")
PY