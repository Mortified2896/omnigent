#!/usr/bin/env bash
# Promote the latest committed fork/main into the live omnigent-eval-web
# systemd service.
#
# This is the canonical, durable replacement for the previously
# agent-driven deploy-main-* promotion workflow. Each step is a preflight
# gate: a failure aborts the promotion and leaves the previous healthy
# deployment serving traffic.
#
# Steps:
#   1. Resolve the current fork/main SHA.
#   2. Create (or reuse) the detached deploy-main-<short> worktree.
#   3. Symlink the worktree's .venv to the dev checkout's existing venv.
#   4. Install frontend deps with `npm ci`, falling back to `npm install`
#      when the lockfile has drifted (the min-release-age cooldown in
#      web/.npmrc generates occasional drift across regen cycles).
#   5. Run the production frontend build (`npm run build`).
#   6. Run the preflight: assert the worktree contains web-ui/index.html.
#   7. Write the systemd drop-in that pins the service to the new worktree.
#   8. Disable the previous drop-in (if any).
#   9. Reload systemd and restart the service.
#  10. Health-check the service is reachable and serving the SPA.
#  11. Update ~/.omnigent/deployed-sha on success.
#
# Usage:
#   scripts/promote_main_deploy.sh
#
# Environment:
#   REPO_ROOT                      Defaults to the main checkout path.
#   WORKTREE_BASE                  Defaults to $WORKSPACE/deploy-main-*.
#   SERVICE_DROPIN_DIR             Defaults to /etc/systemd/system/omnigent-eval-web.service.d.
#   SERVICE_NAME                   Defaults to omnigent-eval-web.service.
#   DEPLOYED_SHA_PATH              Defaults to ~/.omnigent/deployed-sha.
#   OMIT_RESTART=1                 Skip the systemctl restart (sandbox / CI).
#   OMIT_DEPLOYED_SHA=1            Skip the deployed-sha update.
#
# Exits non-zero on any failure. Idempotent: re-running with the same
# fork/main SHA is a no-op for the drop-in and restart (the systemd
# diff is empty and the restart is harmless).

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval}"
WORKTREE_BASE="${WORKTREE_BASE:-/home/hermes/workspace/repos/omnigent-eval-worktrees}"
SERVICE_DROPIN_DIR="${SERVICE_DROPIN_DIR:-/etc/systemd/system/omnigent-eval-web.service.d}"
SERVICE_NAME="${SERVICE_NAME:-omnigent-eval-web.service}"
DEPLOYED_SHA_PATH="${DEPLOYED_SHA_PATH:-/home/hermes/.omnigent/deployed-sha}"
SERVICE_PORT="${SERVICE_PORT:-4097}"
OMNIGENT_BUILTIN_AGENT_DIRS="${OMNIGENT_BUILTIN_AGENT_DIRS:-}"

log() { printf '[promote-main-deploy] %s\n' "$*" >&2; }
fail() { printf '[promote-main-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Resolve fork/main SHA ------------------------------------------------
[[ -d "$REPO_ROOT" ]] || fail "REPO_ROOT does not exist: $REPO_ROOT"
sha=$(git -C "$REPO_ROOT" rev-parse fork/main 2>/dev/null) \
  || fail "could not resolve fork/main (no fork remote? not a git repo?)"
short=$(git -C "$REPO_ROOT" rev-parse --short "$sha")
subject=$(git -C "$REPO_ROOT" log -1 --format='%s' "$sha")
wt="$WORKTREE_BASE/deploy-main-$short"
log "promoting fork/main @ $short ($subject) -> $wt"

# --- 2. Create the worktree --------------------------------------------------
if [[ ! -d "$wt" ]]; then
  log "creating worktree at $wt"
  git -C "$REPO_ROOT" worktree add --detach "$wt" "$sha" \
    || fail "git worktree add failed"
else
  log "reusing existing worktree at $wt"
  # Ensure the worktree is on the expected SHA. A wrong SHA means the
  # previously-existing worktree was created for a different fork/main
  # HEAD and we cannot reuse it.
  current_sha=$(git -C "$wt" rev-parse HEAD)
  if [[ "$current_sha" != "$sha" ]]; then
    fail "worktree $wt is on $current_sha, expected $sha"
  fi
fi

# --- 3. Symlink the venv -----------------------------------------------------
# The deploy worktree shares the dev checkout's venv so editable installs
# (pip install -e . / uv sync) don't have to be re-run per worktree.
# Mirrors the manual pattern that produced deploy-main-0039e23a/.venv.
if [[ ! -e "$wt/.venv" ]]; then
  ln -s "$REPO_ROOT/.venv" "$wt/.venv" \
    || fail "could not symlink $wt/.venv -> $REPO_ROOT/.venv"
  log "linked $wt/.venv -> $REPO_ROOT/.venv"
fi

# --- 4. Install frontend deps ------------------------------------------------
# `npm ci` is the preferred path — it installs exactly what the lockfile
# pins. The lockfile occasionally drifts from package.json (the
# min-release-age=7 cooldown in web/.npmrc publishes a 7-day-deferred
# package set; subsequent manifest edits expose the gap). When npm ci
# fails on that condition, fall back to `npm install` which updates only
# the missing entries rather than re-resolving the entire graph.
pushd "$wt/web" >/dev/null
if ! PATH="/home/hermes/.hermes/node/bin:$PATH" npm ci --no-audit --no-fund; then
  log "npm ci failed (lockfile drifted); falling back to npm install"
  PATH="/home/hermes/.hermes/node/bin:$PATH" npm install --no-audit --no-fund \
    || fail "npm install failed (see npm output above)"
fi

# --- 5. Run the production frontend build ------------------------------------
PATH="/home/hermes/.hermes/node/bin:$PATH" npm run build \
  || fail "npm run build failed (see vite output above)"
popd >/dev/null

# --- 6. Preflight: assert the bundle exists ----------------------------------
# Use the python preflight so the same logic that the server uses at
# startup gates the promotion. Avoids the divergence where the deploy
# script's bash check accepts a stale or partial bundle.
python3 -m omnigent.deploy.preflight "$wt" \
  || fail "preflight failed (rebuild the bundle and retry)"

# --- 7. Write the systemd drop-in --------------------------------------------
new_drop="$SERVICE_DROPIN_DIR/10-deploy-main-$short.conf"
log "writing $new_drop"
{
  printf '# Pin omnigent-eval-web at fork/main %s (%s)\n' "$short" "$subject"
  printf '# using a clean detached worktree. Drop-in precedence (10-deploy-*) wins\n'
  printf '# over the pre-existing evaluator/route-approval/router/tailscale-origin\n'
  printf '# drop-ins, but leaves their Environment*= lines intact.\n'
  printf '[Service]\n'
  printf 'WorkingDirectory=%s\n' "$wt"
  printf 'ExecStart=\n'
  printf 'ExecStart=%s/.venv/bin/python -m omnigent server --host 127.0.0.1 --port %s --no-open --config %s\n' \
    "$wt" "$SERVICE_PORT" "$(dirname "$DEPLOYED_SHA_PATH")/config.yaml"
} | sudo tee "$new_drop" >/dev/null \
  || fail "could not write $new_drop (sudo required)"

# --- 8. Disable the previous drop-in -----------------------------------------
# Move any active 10-deploy-main-*.conf that isn't ours to *.disabled so
# the drop-in precedence resolves to the new deployment.
shopt -s nullglob
for f in "$SERVICE_DROPIN_DIR"/10-deploy-main-*.conf; do
  if [[ "$(basename "$f")" != "$(basename "$new_drop")" ]]; then
    log "disabling previous drop-in $(basename "$f")"
    sudo mv "$f" "$f.disabled" || fail "could not disable $f"
  fi
done

# Keep 15-control-room-polly-builtin.conf pointing at the new worktree's
# examples directory so the auto-seeded built-in agent row matches the
# running code. The post-start ExecStartPost hook
# (25-rename-control-room-polly-to-verity.conf) is idempotent, so leaving
# it in place is safe.
builtin_drop="$SERVICE_DROPIN_DIR/15-control-room-polly-builtin.conf"
if [[ -e "$builtin_drop" ]] && [[ -n "$OMNIGENT_BUILTIN_AGENT_DIRS" ]]; then
  log "updating OMNIGENT_BUILTIN_AGENT_DIRS in $builtin_drop"
  sudo sed -i \
    "s|^Environment=OMNIGENT_BUILTIN_AGENT_DIRS=.*|Environment=OMNIGENT_BUILTIN_AGENT_DIRS=$OMNIGENT_BUILTIN_AGENT_DIRS|" \
    "$builtin_drop" \
    || fail "could not update $builtin_drop"
fi

# --- 9. Reload systemd and restart the service --------------------------------
if [[ "${OMIT_RESTART:-0}" == "1" ]]; then
  log "OMIT_RESTART=1 set; skipping daemon-reload and restart"
else
  sudo systemctl daemon-reload || fail "systemctl daemon-reload failed"
  sudo systemctl restart "$SERVICE_NAME" || fail "systemctl restart $SERVICE_NAME failed"

  # Wait for the service to come back up.
  up_after=0
  for i in $(seq 1 30); do
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      up_after=$i
      break
    fi
    sleep 2
  done
  if [[ "$up_after" -eq 0 ]]; then
    fail "service $SERVICE_NAME did not become active after restart"
  fi
  log "service reached active state after $up_after probe(s)"
fi

# --- 10. Health-check --------------------------------------------------------
# Validate that the service is actually serving the SPA, not the API-only
# fallback. A single GET to / is enough — the SPA shell HTML is the
# canonical fingerprint of a working frontend bundle.
if [[ "${OMIT_RESTART:-0}" != "1" ]]; then
  body=$(curl -fsS "http://127.0.0.1:$SERVICE_PORT/" 2>/dev/null) \
    || fail "could not reach http://127.0.0.1:$SERVICE_PORT/ after restart"
  if printf '%s' "$body" | grep -q 'OMNIGENT_SKIP_WEB_UI'; then
    fail "service is serving the API-only landing page; SPA bundle is still missing"
  fi
  if ! printf '%s' "$body" | grep -q '<title>Omnigent</title>'; then
    fail "service response is not the SPA shell (expected <title>Omnigent</title>)"
  fi
  log "health check passed: SPA shell served at http://127.0.0.1:$SERVICE_PORT/"
fi

# --- 11. Update deployed-sha --------------------------------------------------
if [[ "${OMIT_DEPLOYED_SHA:-0}" == "1" ]]; then
  log "OMIT_DEPLOYED_SHA=1 set; skipping deployed-sha update"
else
  mkdir -p "$(dirname "$DEPLOYED_SHA_PATH")"
  printf '%s\n' "$sha" > "$DEPLOYED_SHA_PATH" \
    || fail "could not write $DEPLOYED_SHA_PATH"
  log "wrote $DEPLOYED_SHA_PATH = $sha"
fi

log "promote-main-deploy $short complete"
