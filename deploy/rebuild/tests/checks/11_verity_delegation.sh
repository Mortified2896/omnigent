#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Check 11 — Verity delegates to the intended harness without
# silent fallback
# ─────────────────────────────────────────────────────────────────────
#
# Boots the wheel with the three agent bundles (verity, pi,
# opencode) registered, posts a session with `agent_selector:
# "verity"` and a prompt that asks Verity to delegate the actual
# implementation to a Pi sub-agent. Then verifies the parent's
# stream carries the full delegation lifecycle:
#
#   1. session.created with the requested selector = "verity"
#   2. sys_session_send{purpose: "implement"} in the parent
#   3. session.created event in the parent for a child session
#      with the *requested* selector (here: "pi")
#   4. session.harness event with payload.harness matching the
#      requested child harness (NOT silently falling back to
#      "claude-sdk" or the default agent)
#   5. session.child_session.updated event in the parent's stream
#   6. sub_agent item in the parent's sys_read_inbox
#
# Pass criteria: the requested child harness name is honored
# end-to-end; the check FAILS if any of the following happens:
#   - The child session's session.harness event names a different
#     harness than the one the user requested (silent fallback).
#   - The child session's session.harness event names the default
#     agent's harness ("claude-sdk") instead of the requested one.
#   - The session.child_session.updated event is absent.
#   - The parent's sys_read_inbox has no sub_agent item.
#
# Requires: python3, curl.
#
# Inputs (env vars):
#   OMNIGENT_PORT       the canary wheel's TCP port
#   OMNIGENT_AUTH_HEADER the auth header name
#   CANARY_IDENTITY     the canary's auth identity
#   CANARY_FIXTURES_ROOT the canary fixtures root
#   CANARY_RUN_ID       the per-run ID
#   REQUESTED_CHILD_HARNESS the harness selector the user asked
#                            Verity to use (default: "pi"). The
#                            check asserts the actual harness in
#                            the child's session.harness event
#                            matches this value. (We also accept
#                            the canonical "pi-native" / "opencode-
#                            native" spellings because upstream's
#                            harness_plugins may rewrite the
#                            selector at the spec-load site.)
#
# Output: prints "PASS" or "FAIL <reason>" on stdout; returns 0
# on PASS, 1 on FAIL.

set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"

: "${OMNIGENT_PORT:=6767}"
: "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
: "${CANARY_IDENTITY:=canary@omnigent.local}"
: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"
: "${REQUESTED_CHILD_HARNESS:=pi}"

ok()  { printf 'PASS\n'; exit 0; }
bad() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

# Map the requested selector to the canonical harness name(s) we
# accept. The user-visible `agent_selector` may be the short form
# (e.g. "pi") or the canonical form (e.g. "pi-native"); both are
# legitimate spellings in the upstream v0.7 wire protocol and the
# harness_plugins map resolves the short form to the canonical
# form at the spec-load site. We accept both.
ACCEPTED_HARNESSES=""
case "$REQUESTED_CHILD_HARNESS" in
  pi|pi-native)
    ACCEPTED_HARNESSES="pi pi-native pi_native" ;;
  opencode|opencode-native)
    ACCEPTED_HARNESSES="opencode opencode-native opencode_native" ;;
  *)
    bad "REQUESTED_CHILD_HARNESS=$REQUESTED_CHILD_HARNESS is not a recognized harness selector" ;;
esac

REPO_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/verity/repo"
WORKTREE_DIR="$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID/verity/worktree"
BRANCH="polly/canary-11-verity-${CANARY_RUN_ID}"

# Fixture repo + worktree.
rm -rf "$REPO_DIR" "$WORKTREE_DIR"
mkdir -p "$REPO_DIR"
git -C "$REPO_DIR" init --quiet --initial-branch=main
git -C "$REPO_DIR" config user.email "canary@omnigent.local"
git -C "$REPO_DIR" config user.name "Canary Bot"
printf 'def foo():\n    return 1\n' >"$REPO_DIR/module.py"
git -C "$REPO_DIR" add module.py
git -C "$REPO_DIR" commit --quiet -m "initial"
git -C "$REPO_DIR" worktree add -b "$BRANCH" "$WORKTREE_DIR" main >/dev/null

# Post the parent session.
SESSIONS_URL="http://127.0.0.1:${OMNIGENT_PORT}/v1/sessions"
# Resolve verity's agent_id (the upstream wire format binds by
# agent_id, not by name; resolve_agent_id lives in _harness_lib.sh).
. "$HERE/_harness_lib.sh"
VERITY_AGENT_ID=$(resolve_agent_id "${OMNIGENT_PORT}" "${OMNIGENT_AUTH_HEADER}" "${CANARY_IDENTITY}" "verity")
[ -n "$VERITY_AGENT_ID" ] || bad "could not resolve verity agent_id on port ${OMNIGENT_PORT}"
BODY=$(cat <<JSON
{
  "agent_id": "${VERITY_AGENT_ID}",
  "workspace": "${WORKTREE_DIR}",
  "prompt": "rename foo to bar in module.py using a ${REQUESTED_CHILD_HARNESS} sub-agent. Push the branch when green.",
  "title": "canary-11-verity-${CANARY_RUN_ID}"
}
JSON
)

# POST and capture status code separately so we can FAIL on non-2xx
# instead of curl aborting.
HTTP_CODE=$(curl -sS -o /tmp/canary-11-session.json -w '%{http_code}' --max-time 30 \
  -H "Content-Type: application/json" \
  -H "${OMNIGENT_AUTH_HEADER}: ${CANARY_IDENTITY}" \
  -X POST -d "$BODY" "$SESSIONS_URL" || true)
case "$HTTP_CODE" in
  2*) ;;
  *) bad "POST /v1/sessions returned HTTP $HTTP_CODE; body: $(head -c 200 /tmp/canary-11-session.json)" ;;
esac
SESSION_ID=$(python3 -c "import sys, json; print(json.load(open('/tmp/canary-11-session.json'))['id'])")

# Wait up to 240 s for the parent session to finish.
DEADLINE=$((SECONDS + 240))
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  STATUS=$(curl -fsS --max-time 5 \
    -H "${OMNIGENT_AUTH_HEADER}: ${CANARY_IDENTITY}" \
    "http://127.0.0.1:${OMNIGENT_PORT}/v1/sessions/${SESSION_ID}" \
    | python3 -c "import sys, json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)
  case "$STATUS" in
    finished) break ;;
    failed)   bad "verity session failed (status=failed)" ;;
  esac
  sleep 2
done
[ "$SECONDS" -lt "$DEADLINE" ] || bad "verity session did not finish within 240 s"

# Read the full session JSON.
curl -fsS --max-time 10 \
  -H "${OMNIGENT_AUTH_HEADER}: ${CANARY_IDENTITY}" \
  "http://127.0.0.1:${OMNIGENT_PORT}/v1/sessions/${SESSION_ID}" \
  >/tmp/canary-11-full.json

# Verify the lifecycle events. This is the silent-fallback guard:
# we must observe (1) the parent's session.created with selector
# "verity", (2) a sys_session_send{purpose=implement}, (3) a
# child session.created with the requested child harness selector,
# (4) a session.harness event whose payload.harness is one of the
# accepted spellings of the requested child harness (NOT
# "claude-sdk" or the default agent), (5) a
# session.child_session.updated event, and (6) a sub_agent item
# in the parent's sys_read_inbox.
REQUESTED="$REQUESTED_CHILD_HARNESS" python3 - "$ACCEPTED_HARNESSES" "$REQUESTED_CHILD_HARNESS" </tmp/canary-11-full.json <<'PY' || bad "verity lifecycle check failed (see evidence)"
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except json.JSONDecodeError as exc:
    print(f"FAIL: cannot parse session JSON: {exc}")
    sys.exit(1)
events = data.get("events", [])

# Build the accepted-harness set from argv[1] (space-separated list).
accepted = set(sys.argv[1].split())
requested = sys.argv[2]

def has_event(predicate):
    for ev in events:
        if predicate(ev):
            return True
    return False

# 1. Parent session.created with selector "verity".
if not has_event(lambda ev: ev.get("type") == "session.created" and ev.get("payload", {}).get("agent_selector") == "verity"):
    print("FAIL no session.created with agent_selector=verity")
    sys.exit(1)

# 2. sys_session_send with purpose=implement.
if not has_event(lambda ev: "sys_session_send" in (ev.get("type") or "") and ev.get("payload", {}).get("purpose") == "implement"):
    print("FAIL no sys_session_send with purpose=implement")
    sys.exit(1)

# 3. Child session.created with the requested selector.
if not has_event(lambda ev: ev.get("type") == "session.created" and ev.get("payload", {}).get("agent_selector") in accepted):
    print(f"FAIL no child session.created with agent_selector in {sorted(accepted)}")
    sys.exit(1)

# 4. session.harness event whose payload.harness is in the
#    accepted set. This is the strict silent-fallback guard: we
#    REFUSE to PASS if the child ended up on any other harness,
#    including "claude-sdk" (the default agent's harness) or any
#    other -native spelling not in the accepted set.
harness_events = [ev for ev in events if ev.get("type") == "session.harness"]
if not harness_events:
    print("FAIL no session.harness event in the parent stream")
    sys.exit(1)
for ev in harness_events:
    h = (ev.get("payload") or {}).get("harness", "")
    if h in accepted:
        print(f"PASS  child harness: {h} matches requested {requested}")
        break
    print(f"  note: session.harness event saw harness={h!r}, does not match requested {requested!r}")
else:
    print(f"FAIL every session.harness event used a harness not in {sorted(accepted)}; silent fallback detected")
    sys.exit(1)

# 5. session.child_session.updated event.
if not has_event(lambda ev: ev.get("type") == "session.child_session.updated"):
    print("FAIL no session.child_session.updated event")
    sys.exit(1)

# 6. sub_agent item in the parent's sys_read_inbox.
inbox = data.get("inbox", [])
if not any(it.get("type") == "sub_agent" for it in inbox):
    print("FAIL parent's inbox has no sub_agent item")
    sys.exit(1)

# All checks passed.
PY

# Emit evidence block (will be captured by the runner).
cat <<EVIDENCE
parent_session_id=$SESSION_ID
requested_child_harness=$REQUESTED_CHILD_HARNESS
accepted_harness_spellings=$ACCEPTED_HARNESSES
worktree=$WORKTREE_DIR
branch=$BRANCH
EVIDENCE

ok
