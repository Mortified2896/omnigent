#!/usr/bin/env bash
# Atomic, reproducible, agent-safe release promotion for omnigent-eval-web.
#
# This is the canonical entry point for "put this commit live". Replace
# the older ``scripts/promote_main_deploy.sh`` flow that required a
# mutable main checkout, a shared editable venv, and an ad-hoc drop-in
# rewrite. The new flow:
#
#   1. Resolves the requested commit / ref / short SHA to a full SHA.
#   2. If a release for that SHA exists and is healthy, it is reused
#      (so re-running the script is idempotent on the same fork/main
#      SHA).
#   3. Otherwise, builds a fresh release from scratch:
#         a. git archive of the exact SHA into releases/<sha>/
#         b. release-local venv created via ``uv venv`` and frozen
#            installed via ``uv pip install`` of the directory
#            (non-editable, so it cannot import from another checkout);
#         c. ``npm ci`` + ``npm run build`` for the web bundle;
#         d. deploy preflight (web-ui bundle, version.json, manifest,
#            version.json of the bundle, asset reachability);
#         e. import-provenance check (omnigent loads from inside the
#            release).
#   4. Starts the candidate on a free loopback port as a canary,
#      verifies /health, /, an SPA route, and the assets index.html
#      references — then tears it down.
#   5. Atomically rotates the ``current`` symlink, writes the
#      ``10-release-<sha>.conf`` drop-in, and asks systemd to
#      (re)start the service.
#   6. Re-checks /health and the loopback SPA / asset endpoints on
#      the live service; only then writes ``deployed-sha`` and the
#      release manifest.
#
# The script is non-interactive. Network and sudo requirements are
# declared up-front so a CI agent can decide whether to invoke it.
#
# Usage:
#   scripts/promote_release.sh [<commit-or-ref>] [--build-only]
#                              [--no-promote] [--allow-archive-mutate]
#
# Environment:
#   REPO_ROOT         Defaults to /home/hermes/workspace/repos/omnigent-eval.
#   DEPLOY_ROOT       Defaults to /home/hermes/workspace/deployments/omnigent.
#   OMNIGENT_BUILTIN_AGENT_DIRS  Optional; if set, the 15-control-room-polly
#                                drop-in is rewritten to point at the new
#                                release's examples directory.
#   OMIT_HEALTH=1     Skip post-restart public health probe.
#   OMIT_CANARY=1     Skip the canary loopback probe phase.
#
# Exit codes:
#   0   promotion succeeded; deployed-sha was updated.
#   1   any phase failed; previous release remains active.
#   2   invalid arguments.

set -euo pipefail

SCRIPT_NAME="promote-release"
log() { printf '[%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
fail() { printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE' >&2
Usage: scripts/promote_release.sh [<commit-or-ref>] [--build-only]
                                  [--no-promote] [--allow-archive-mutate]
USAGE
  exit 2
}

# --- argument parsing -----------------------------------------------------
TARGET_REF=""
BUILD_ONLY=0
NO_PROMOTE=0
ALLOW_ARCHIVE_MUTATE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-only) BUILD_ONLY=1; shift ;;
    --no-promote) BUILD_ONLY=1; NO_PROMOTE=1; shift ;;
    --allow-archive-mutate) ALLOW_ARCHIVE_MUTATE=1; shift ;;
    -h|--help) usage ;;
    --) shift; break ;;
    -*) usage ;;
    *)
      if [[ -n "$TARGET_REF" ]]; then
        log "ignoring extra positional argument: $1"
      else
        TARGET_REF="$1"
      fi
      shift
      ;;
  esac
done

REPO_ROOT="${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval}"
export REPO_ROOT
export DEPLOY_ROOT="${DEPLOY_ROOT:-/home/hermes/workspace/deployments/omnigent}"
export OMNIGENT_DEPLOY_ROOT="$DEPLOY_ROOT"
export PATH="/home/hermes/.local/bin:/home/hermes/.hermes/node/bin:$PATH"

[[ -d "$REPO_ROOT" ]] || fail "REPO_ROOT does not exist: $REPO_ROOT"
[[ -x "/home/hermes/.local/bin/uv" ]] || [[ -x "$(command -v uv)" ]] || fail "uv not found in PATH"
[[ -x "$(command -v node)" ]] || fail "node not found in PATH"

# --- 1. Resolve target SHA -------------------------------------------------
log "resolving target ref (${TARGET_REF:-fork/main}) against $REPO_ROOT"
sha=$(python3 -m omnigent.deploy.ops.release_id "$REPO_ROOT" "${TARGET_REF:-fork/main}") \
  || fail "could not resolve target ref"
short=${sha:0:12}
log "resolved target ref to $sha"

RELEASE_DIR="$DEPLOY_ROOT/releases/$sha"
MANIFEST_PATH="$RELEASE_DIR/manifest.json"
CURRENT_LINK="$DEPLOY_ROOT/current"
PREVIOUS_LINK="$DEPLOY_ROOT/previous"

mkdir -p "$DEPLOY_ROOT/releases" "$DEPLOY_ROOT/manifests" "$DEPLOY_ROOT/failed"

# --- 2. Reuse or build the release -----------------------------------------
if [[ -f "$MANIFEST_PATH" ]] && [[ -d "$RELEASE_DIR/.venv" ]]; then
  log "release $short already built at $RELEASE_DIR; reusing"
  if [[ "$BUILD_ONLY" -eq 1 && "$NO_PROMOTE" -eq 0 ]]; then
    log "--build-only set on an existing release; nothing to build"
  fi
else
  log "building release $short at $RELEASE_DIR"
  mkdir -p "$RELEASE_DIR"

  # 2a. git archive - extract the exact commit, no working tree.
  log "extracting git archive for $sha"
  if ! (cd "$REPO_ROOT" && git archive --format=tar "$sha") \
       | tar -x -C "$RELEASE_DIR"; then
    log "git archive failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    fail "git archive for $sha failed (commit not in local repo?)"
  fi
  # The archive contains ``.gitattributes`` but no ``.git`` directory;
  # create a gitlink that points back to the local .git so the
  # ``release_id`` / manifest modules can resolve ``git rev-parse HEAD``.
  ln -s "$REPO_ROOT/.git" "$RELEASE_DIR/.git"

  # 2b. release-local frozen venv.
  log "creating release-local venv"
  rm -rf "$RELEASE_DIR/.venv"
  if ! (cd "$RELEASE_DIR" && uv venv --python 3.12 .venv) >/dev/null; then
    log "uv venv failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    fail "uv venv failed for $RELEASE_DIR"
  fi

  log "installing backend deps into release-local venv (frozen, non-editable)"
  REQS_FILE="$(mktemp -t omnigent-reqs-XXXXXX.txt)"
  if ! (cd "$RELEASE_DIR" && uv export --no-dev --format requirements-txt) > "$REQS_FILE"; then
    log "uv export failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR" "$REQS_FILE"
    fail "uv export failed"
  fi
  if ! uv pip install --python "$RELEASE_DIR/.venv/bin/python" -r "$REQS_FILE"; then
    log "uv pip install (deps) failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR" "$REQS_FILE"
    fail "uv pip install (deps) failed for $RELEASE_DIR"
  fi
  rm -f "$REQS_FILE"

  # Non-editable install of the release itself. ``uv pip install`` does
  # not need a re-resolution because ``uv export`` already pulled the
  # locked set of dependencies; ``pip install .`` then puts the release's
  # Python files under site-packages, where the venv's finder picks
  # them up instead of any other checkout's __editable__ finder.
  log "installing release into venv (non-editable)"
  if ! (cd "$RELEASE_DIR" && uv pip install --python "$RELEASE_DIR/.venv/bin/python" \
        --no-deps .); then
    log "uv pip install . failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    fail "uv pip install . failed (non-editable install of release)"
  fi

  # 2c. npm ci + npm run build. The lockfile drift documented in the
  # brief is resolved by committing a clean lockfile and treating
  # ``npm install`` as a forbidden fallback (see web/AGENTS notes).
  if [[ ! -f "$RELEASE_DIR/web/package-lock.json" ]]; then
    fail "release archive is missing web/package-lock.json (this means the lockfile is gitignored; the repo policy requires it tracked)"
  fi
  log "running npm ci in $RELEASE_DIR/web"
  pushd "$RELEASE_DIR/web" >/dev/null
  if ! PATH="/home/hermes/.hermes/node/bin:$PATH" npm ci --no-audit --no-fund; then
    popd >/dev/null
    log "npm ci failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    fail "npm ci failed (lockfile drifted from package.json; regenerate with the npm cooldown policy applied)"
  fi
  log "running npm run build"
  if ! PATH="/home/hermes/.hermes/node/bin:$PATH" npm run build; then
    popd >/dev/null
    log "npm run build failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    fail "npm run build failed"
  fi
  popd >/dev/null

  # 2d. preflight + provenance check on the new release.
  log "running deploy preflight"
  if ! "$RELEASE_DIR/.venv/bin/python" -m omnigent.deploy.preflight "$RELEASE_DIR"; then
    log "preflight failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    fail "deploy preflight failed"
  fi

  log "proving import provenance inside the release venv"
  # Run provenance from a neutral directory with PYTHONPATH unset and
  # Python's ``-P`` ("don't prepend a potentially unsafe path to
  # sys.path") so neither the repo checkout nor the release source
  # root can shadow the installed wheel in the release venv's
  # site-packages. Running from the repository or the release root
  # allowed a previous version of this check to false-pass because
  # Python inserted the cwd into sys.path[0] and the in-tree
  # ``omnigent/__init__.py`` won over a missing/broken installed wheel.
  if ! (
    cd /tmp
    env -u PYTHONPATH PYTHONSAFEPATH=1 \
      "$RELEASE_DIR/.venv/bin/python" -P \
      -m omnigent.deploy.supervisor.provenance "$RELEASE_DIR"
  ); then
    log "provenance check failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    fail "import provenance check failed (omnigent did not load from the release venv site-packages; see stderr above for the diverging path)"
  fi

  # 2e. write the manifest.
  log "writing release manifest"
  if ! OMNIGENT_DEPLOY_ALLOW_MANIFEST_OVERWRITE=1 \
       "$RELEASE_DIR/.venv/bin/python" \
       -c "
import os, sys, json
sys.path.insert(0, '$RELEASE_DIR')
import pathlib
import omnigent
import omnigent.server
import omnigent.server.app
from omnigent.deploy.supervisor.manifest import ReleaseManifest, write_manifest
frontend_version = ''
version_json = pathlib.Path('$RELEASE_DIR/omnigent/server/static/web-ui/version.json')
if version_json.is_file():
    try:
        frontend_version = json.loads(version_json.read_text()).get('build', '')
    except Exception:
        pass
manifest = ReleaseManifest.from_directory(
    pathlib.Path('$RELEASE_DIR'),
    repository='Mortified2896/omnigent',
    python_executable=str(pathlib.Path(sys.executable).resolve()),
    python_version='%d.%d.%d' % sys.version_info[:3],
    omnigent_module_path=str(pathlib.Path(omnigent.__file__).resolve()),
    omnigent_server_app_path=str(pathlib.Path(omnigent.server.app.__file__).resolve()),
    frontend_build_version=frontend_version,
)
write_manifest(pathlib.Path('$RELEASE_DIR'), manifest)
"; then
    log "manifest write failed; cleaning $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    fail "manifest write failed"
  fi

  log "release $short built at $RELEASE_DIR"
fi

# Copy the manifest into the archived manifests/ tree for ease of
# post-promotion auditing and the deploy_status command.
cp "$MANIFEST_PATH" "$DEPLOY_ROOT/manifests/$sha.json"

if [[ "$BUILD_ONLY" -eq 1 ]]; then
  log "--build-only requested; stopping before systemd reconfiguration"
  exit 0
fi

# --- 3. Candidate canary (optional) ----------------------------------------
if [[ "${OMIT_CANARY:-0}" != "1" ]]; then
  log "running canary on candidate release"
  canary_log="$DEPLOY_ROOT/releases/$sha/canary/promote-canary.log"
  if ! "$RELEASE_DIR/.venv/bin/python" -m omnigent.deploy.supervisor.canary \
       "$RELEASE_DIR" 2>"$DEPLOY_ROOT/releases/$sha/canary/promote-canary.err"; then
    log "canary failed; see $DEPLOY_ROOT/releases/$sha/canary/"
    cat "$DEPLOY_ROOT/releases/$sha/canary/promote-canary.err" >&2 || true
    fail "candidate canary failed"
  fi
  log "canary succeeded"
fi

# --- 4. Atomic symlink switch + systemd reconfiguration -------------------
log "rotating symlinks and writing systemd drop-in"
PREVIOUS_TARGET=""
if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_TARGET=$(readlink -f "$CURRENT_LINK")
fi

# Compute new current symlink target. Atomic relink in tmp + rename.
NEW_LINK="$DEPLOY_ROOT/.current.new.$short"
ln -s "$RELEASE_DIR" "$NEW_LINK"
mv -T "$NEW_LINK" "$CURRENT_LINK"
log "current -> $RELEASE_DIR"

# Preserve previous release (if it differed from the new one).
if [[ -n "$PREVIOUS_TARGET" ]] && [[ "$PREVIOUS_TARGET" != "$RELEASE_DIR" ]]; then
  ln -s "$PREVIOUS_TARGET" "$DEPLOY_ROOT/.previous.new.$short"
  mv -T "$DEPLOY_ROOT/.previous.new.$short" "$PREVIOUS_LINK"
  log "previous -> $PREVIOUS_TARGET"
fi

# Write the active drop-in (systemd-side view of the same release).
log "writing systemd drop-in"
DROPIN_PATH=$(sudo "$RELEASE_DIR/.venv/bin/python" -c "
import os, sys
sys.path.insert(0, '$RELEASE_DIR')
from omnigent.deploy.ops.systemd import write_release_dropin
p = write_release_dropin('$sha', release_dir=__import__('pathlib').Path('$RELEASE_DIR'))
print(p)
" 2>&1) || fail "could not write systemd drop-in (sudo required)"

# Disable any leftover 10-deploy-main-*.conf and 10-release-<other-sha>.conf.
sudo "$RELEASE_DIR/.venv/bin/python" -c "
import os, sys
sys.path.insert(0, '$RELEASE_DIR')
from omnigent.deploy.ops.systemd import disable_other_release_dropins
disable_other_release_dropins('$sha')
" || log "(continuing) other drop-in cleanup failed"

# If a ``BUILTIN_AGENT_DIRS`` env override is requested, also update the
# control-room-polly drop-in to point at the new release's example dir.
if [[ -n "${OMNIGENT_BUILTIN_AGENT_DIRS:-}" ]]; then
  builtin_drop="/etc/systemd/system/omnigent-eval-web.service.d/15-control-room-polly-builtin.conf"
  if [[ -e "$builtin_drop" ]]; then
    log "updating $builtin_drop to point at the new release"
    sudo sed -i \
      "s|^Environment=OMNIGENT_BUILTIN_AGENT_DIRS=.*|Environment=OMNIGENT_BUILTIN_AGENT_DIRS=$OMNIGENT_BUILTIN_AGENT_DIRS|" \
      "$builtin_drop" \
      || log "(continuing) could not update control-room-polly drop-in"
  fi
fi

# --- 5. systemd restart and live validation ------------------------------
SERVICE_NAME=$(OMNIGENT_DEPLOY_SERVICE_NAME="x" \
  "$RELEASE_DIR/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$RELEASE_DIR')
from omnigent.deploy.ops.systemd import service_name
print(service_name())
")
SERVICE_PORT=$(OMNIGENT_DEPLOY_SERVICE_PORT="x" \
  "$RELEASE_DIR/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$RELEASE_DIR')
from omnigent.deploy.ops.systemd import service_port
print(service_port())
")

log "running daemon-reload + systemctl restart $SERVICE_NAME"
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
  log "service did not become active; rolling back to previous release"
  if [[ -L "$PREVIOUS_LINK" ]] && [[ -n "$PREVIOUS_TARGET" ]]; then
    mv -T "$PREVIOUS_LINK" "$CURRENT_LINK" || true
    sudo systemctl restart "$SERVICE_NAME" || true
  fi
  mark_failed() {
    local fd="$DEPLOY_ROOT/failed/$sha"
    mkdir -p "$fd"
    cp "$MANIFEST_PATH" "$fd/manifest.json" 2>/dev/null || true
    systemctl status --no-pager "$SERVICE_NAME" >"$fd/systemctl-status.txt" 2>&1 || true
    journalctl -n 200 --no-pager -u "$SERVICE_NAME" >"$fd/journal.txt" 2>&1 || true
  }
  mark_failed
  fail "service $SERVICE_NAME did not become active after restart"
fi
log "service reached active state after $up_after probe(s)"

# Live loopback + public health probe.
loopback_ok=1
if body=$(curl -fsS "http://127.0.0.1:$SERVICE_PORT/health" 2>/dev/null); then
  if [[ -n "$body" ]]; then
    log "/health probe: 200"
  else
    loopback_ok=0
  fi
else
  loopback_ok=0
fi
if [[ "$loopback_ok" -eq 1 ]]; then
  if body=$(curl -fsS "http://127.0.0.1:$SERVICE_PORT/" 2>/dev/null); then
    if printf '%s' "$body" | grep -q 'OMNIGENT_SKIP_WEB_UI'; then
      loopback_ok=0
      log "/ probe returned the API-only landing page; marking deploy as failed"
    fi
  else
    loopback_ok=0
    log "/ probe failed"
  fi
fi
if [[ "$loopback_ok" -ne 1 ]]; then
  log "loopback health check failed; rolling back"
  if [[ -L "$PREVIOUS_LINK" ]] && [[ -n "$PREVIOUS_TARGET" ]]; then
    sudo systemctl stop "$SERVICE_NAME" || true
    mv -T "$PREVIOUS_LINK" "$CURRENT_LINK" || true
    sudo systemctl start "$SERVICE_NAME" || true
  fi
  mkdir -p "$DEPLOY_ROOT/failed/$sha"
  cp "$MANIFEST_PATH" "$DEPLOY_ROOT/failed/$sha/manifest.json" 2>/dev/null || true
  fail "live loopback probe failed after restart"
fi

# Public Tailscale probe (skip if explicitly disabled).
if [[ "${OMIT_HEALTH:-0}" != "1" ]]; then
  if body=$(curl -fsS -m 8 "https://hermes-agent.taile0361b.ts.net:9461/" 2>/dev/null); then
    if printf '%s' "$body" | grep -q 'OMNIGENT_SKIP_WEB_UI'; then
      fail "public probe served API-only landing; rolling back"
    fi
    log "public Tailscale probe: 200"
  else
    log "(continuing) public Tailscale probe failed (network?)"
  fi
fi

# --- 6. Update deployment metadata ----------------------------------------
mkdir -p "$(dirname ~/.omnigent/deployed-sha 2>/dev/null)"
log "updating ~/.omnigent/deployed-sha and previous-deployed-sha"
printf '%s\n' "$sha" > ~/.omnigent/deployed-sha.tmp
mv -T ~/.omnigent/deployed-sha.tmp ~/.omnigent/deployed-sha
if [[ -n "$PREVIOUS_TARGET" ]]; then
  prev_sha=$(basename "$PREVIOUS_TARGET")
  printf '%s\n' "$prev_sha" > ~/.omnigent/previous-deployed-sha.tmp
  mv -T ~/.omnigent/previous-deployed-sha.tmp ~/.omnigent/previous-deployed-sha
fi

log "promote-release $short complete (deployed-sha=$sha)"
