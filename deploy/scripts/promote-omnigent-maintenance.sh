#!/usr/bin/env bash
# Promote only the exact artifact already accepted on Omnigent 2 to Omnigent 1.
set -euo pipefail
shopt -s nullglob

PROD_ROOT="${OMNIGENT_PROD_RELEASE_ROOT:-/opt/omnigent-production}"
MAINT_ROOT="${OMNIGENT_MAINT_RELEASE_ROOT:-/opt/omnigent}"
MAINT_HEALTH_URL="${OMNIGENT_MAINT_HEALTH_URL:-http://127.0.0.1:4097/health}"
MAINT_BASE_URL="${MAINT_HEALTH_URL%/health}"
MAINT_HEALTH_TIMEOUT_S="${OMNIGENT_MAINT_HEALTH_TIMEOUT_S:-60}"
OMNIGENT_MAINT_ACCEPTANCE_HOOK_DIR="${OMNIGENT_MAINT_ACCEPTANCE_HOOK_DIR:-}"
OMNIGENT_RELEASE_PREFLIGHT="${OMNIGENT_RELEASE_PREFLIGHT:-/home/hermes/workspace/repos/omnigent-2-production/deploy/scripts/omnigent_release_preflight.py}"
PROD_MARKER="$PROD_ROOT/maintenance-candidate.sha"
MAINT_MARKER="$MAINT_ROOT/DEPLOYED_SHA"
SERVER_UNIT="${OMNIGENT_MAINT_SERVER_UNIT:-omnigent.service}"
HOST_UNIT="${OMNIGENT_MAINT_HOST_UNIT:-omnigent-host.service}"
PROD_SERVICES=(omnigent-production.service omnigent-production-host.service)
SUDO=()

log() { printf '[promote-omnigent-maintenance] %s\n' "$*" >&2; }
refuse() { printf '[promote-omnigent-maintenance] REFUSED: %s\n' "$*" >&2; exit 2; }
fail() { printf '[promote-omnigent-maintenance] FAILED: %s\n' "$*" >&2; exit 4; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || refuse "missing required command: $1"; }
require_exact_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]] || refuse "expected an exact 40-character lowercase commit SHA"; }
reject_web_ui_skip() {
  [[ ! ${OMNIGENT_SKIP_WEB_UI+x} ]] || refuse "OMNIGENT_SKIP_WEB_UI must be unset"
  [[ ! ${OMNIAGENTS_SKIP_WEB_UI+x} ]] || refuse "OMNIAGENTS_SKIP_WEB_UI must be unset"
}
resolve_cmd() {
  local found
  found=$(command -v "$1" 2>/dev/null || true)
  [[ -n "$found" ]] || [[ ! -x "$HOME/.local/bin/$1" ]] || found="$HOME/.local/bin/$1"
  [[ -n "$found" ]] || [[ ! -x "/home/hermes/.local/bin/$1" ]] || found="/home/hermes/.local/bin/$1"
  [[ -n "$found" ]] || refuse "missing required command: $1"
  readlink -f "$found"
}
resolve_python312() {
  local candidate="/home/hermes/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12"
  [[ ! -x "$candidate" ]] || { printf '%s\n' "$candidate"; return; }
  command -v python3.12 >/dev/null 2>&1 || refuse "Python 3.12 is required"
  command -v python3.12
}
require_root_or_sudo() {
  if [[ $EUID -ne 0 ]]; then
    require_cmd sudo
    sudo -n true >/dev/null 2>&1 || refuse "passwordless sudo is required for controlled promotion"
    SUDO=(sudo -E)
  fi
}
preflight_tooling() {
  local command
  for command in curl sha256sum mktemp mv ln date cmp python3 systemctl; do require_cmd "$command"; done
  [[ -x "$OMNIGENT_RELEASE_PREFLIGHT" ]] || refuse "missing executable preflight: $OMNIGENT_RELEASE_PREFLIGHT"
  UV_PATH=$(resolve_cmd uv)
  PYTHON_PATH=$(resolve_python312)
  "$PYTHON_PATH" -c 'import sys; assert sys.version_info[:2] == (3, 12)' \
    || refuse "Python 3.12 check failed"
  "${SUDO[@]}" systemctl cat "$SERVER_UNIT" >/dev/null 2>&1 || refuse "$SERVER_UNIT not installed"
  "${SUDO[@]}" systemctl cat "$HOST_UNIT" >/dev/null 2>&1 || refuse "$HOST_UNIT not installed"
}

capture_production_state() {
  local output="$1" unit
  : > "$output"
  for unit in "${PROD_SERVICES[@]}"; do
    systemctl show "$unit" -p MainPID -p NRestarts -p ActiveEnterTimestampMonotonic >> "$output" || return 1
  done
}
wait_healthy() {
  local deadline=$(( $(date +%s) + $1 ))
  while (( $(date +%s) < deadline )); do
    curl -fsS --max-time 3 "$MAINT_HEALTH_URL" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
restart_maintenance_units() {
  "${SUDO[@]}" systemctl restart "$SERVER_UNIT" || return 1
  wait_healthy "$MAINT_HEALTH_TIMEOUT_S" || return 1
  "${SUDO[@]}" systemctl restart "$HOST_UNIT" || return 1
}

create_maintenance_release() {
  local sha="$1" source="$PROD_ROOT/releases/$sha" final="$MAINT_ROOT/releases/$sha"
  "$OMNIGENT_RELEASE_PREFLIGHT" release "$source" "$sha" >/dev/null \
    || refuse "production candidate failed release preflight"
  if [[ -e "$final" ]]; then
    [[ -f "$final/.complete" ]] && "$OMNIGENT_RELEASE_PREFLIGHT" release "$final" "$sha" >/dev/null \
      || refuse "existing maintenance release failed integrity checks; refusing unsafe reuse"
    RELEASE_DIR="$final"
    return
  fi

  local staging_root="$MAINT_ROOT/releases/.staging-$sha-$$"
  local staging="$staging_root/$sha"
  [[ ! -e "$staging_root" ]] || refuse "staging path already exists: $staging_root"
  "${SUDO[@]}" mkdir -p "$staging/artifacts"
  trap '"${SUDO[@]}" rm -rf "$staging_root"' EXIT

  local wheel_name wheel
  wheel_name=$(python3 - "$source/PROVENANCE.txt" <<'PY'
import sys
print(dict(line.rstrip().split("=", 1) for line in open(sys.argv[1]) if "=" in line)["wheel_filename"])
PY
)
  wheel="$source/artifacts/$wheel_name"
  [[ -f "$wheel" ]] || refuse "accepted production wheel is missing: $wheel"

  # Copy the *same* application artifact that O2 accepted. Never rebuild from a
  # branch tip during promotion.
  "${SUDO[@]}" cp "$wheel" "$staging/artifacts/$wheel_name"
  "${SUDO[@]}" "$UV_PATH" venv --python "$PYTHON_PATH" "$staging/venv" \
    || refuse "maintenance venv creation failed"
  "${SUDO[@]}" "$UV_PATH" pip install --python "$staging/venv/bin/python" \
    "$staging/artifacts/$wheel_name[all]" || refuse "maintenance wheel install failed"
  "${SUDO[@]}" cp "$source/PROVENANCE.txt" "$staging/PROVENANCE.txt"
  {
    printf 'promoted_from=production\n'
    printf 'promoted_at_utc=%s\n' "$(date -u +%FT%TZ)"
  } | "${SUDO[@]}" tee -a "$staging/PROVENANCE.txt" >/dev/null

  "$OMNIGENT_RELEASE_PREFLIGHT" release "$staging" "$sha" >/dev/null \
    || refuse "installed maintenance release preflight failed"
  "${SUDO[@]}" touch "$staging/.complete"
  "${SUDO[@]}" chmod a-w "$staging/PROVENANCE.txt" "$staging/artifacts/$wheel_name"
  "${SUDO[@]}" mv -T "$staging" "$final"
  "${SUDO[@]}" rmdir "$staging_root"
  trap - EXIT
  RELEASE_DIR="$final"
}

switch_venv() {
  local target="$1"
  PREVIOUS_KIND=none PREVIOUS_TARGET=""
  if [[ -L "$MAINT_ROOT/venv" ]]; then
    PREVIOUS_KIND=symlink
    PREVIOUS_TARGET=$(readlink -f "$MAINT_ROOT/venv")
  elif [[ -d "$MAINT_ROOT/venv" ]]; then
    PREVIOUS_KIND=directory
    PREVIOUS_TARGET="$MAINT_ROOT/venv.legacy-$(date -u +%Y%m%dT%H%M%SZ)"
    "${SUDO[@]}" mv "$MAINT_ROOT/venv" "$PREVIOUS_TARGET"
  elif [[ -e "$MAINT_ROOT/venv" ]]; then
    refuse "$MAINT_ROOT/venv is neither a directory nor a symlink"
  fi
  "${SUDO[@]}" ln -sfn "$target/venv" "$MAINT_ROOT/.venv.tmp"
  "${SUDO[@]}" mv -T "$MAINT_ROOT/.venv.tmp" "$MAINT_ROOT/venv"
}
restore_previous() {
  case "$PREVIOUS_KIND" in
    symlink)
      "${SUDO[@]}" ln -sfn "$PREVIOUS_TARGET" "$MAINT_ROOT/.venv.tmp"
      "${SUDO[@]}" mv -T "$MAINT_ROOT/.venv.tmp" "$MAINT_ROOT/venv"
      ;;
    directory)
      "${SUDO[@]}" rm -f "$MAINT_ROOT/venv"
      "${SUDO[@]}" mv "$PREVIOUS_TARGET" "$MAINT_ROOT/venv"
      ;;
    none)
      "${SUDO[@]}" rm -f "$MAINT_ROOT/venv"
      ;;
  esac
}

run_acceptance() {
  local release="$1" sha="$2" production_before="$3"
  local body info installed_index hook production_after
  "$OMNIGENT_RELEASE_PREFLIGHT" release "$release" "$sha" >/dev/null || return 1
  systemctl is-active --quiet "$SERVER_UNIT" || return 1
  systemctl is-active --quiet "$HOST_UNIT" || return 1
  wait_healthy "$MAINT_HEALTH_TIMEOUT_S" || return 1

  body=$(mktemp /tmp/omnigent-maint-root.XXXXXX)
  info=$(mktemp /tmp/omnigent-maint-info.XXXXXX)
  curl -fsS --max-time 10 "$MAINT_BASE_URL/" > "$body" \
    || { rm -f "$body" "$info"; return 1; }
  installed_index=$("$release/venv/bin/python" -c \
    'from pathlib import Path; import omnigent; print(Path(omnigent.__file__).parent / "server/static/web-ui/index.html")') \
    || { rm -f "$body" "$info"; return 1; }
  cmp -s "$installed_index" "$body" \
    || { log "runtime root does not match promoted installed SPA"; rm -f "$body" "$info"; return 1; }

  curl -fsS --max-time 10 "$MAINT_BASE_URL/v1/info" > "$info" \
    || { rm -f "$body" "$info"; return 1; }
  python3 - "$info" "$release/PROVENANCE.txt" <<'PY' \
    || { rm -f "$body" "$info"; return 1; }
import json, sys
info = json.load(open(sys.argv[1]))
provenance = dict(line.rstrip().split("=", 1) for line in open(sys.argv[2]) if "=" in line)
assert info["server_version"] == provenance["package_version"]
PY

  production_after=$(mktemp /tmp/omnigent-prod-after.XXXXXX)
  capture_production_state "$production_after" \
    || { rm -f "$body" "$info" "$production_after"; return 1; }
  cmp -s "$production_before" "$production_after" \
    || { log "O2 service state changed during O1 promotion"; rm -f "$body" "$info" "$production_after"; return 1; }
  rm -f "$body" "$info" "$production_after"

  if [[ -n "$OMNIGENT_MAINT_ACCEPTANCE_HOOK_DIR" ]]; then
    [[ -d "$OMNIGENT_MAINT_ACCEPTANCE_HOOK_DIR" ]] || return 1
    for hook in "$OMNIGENT_MAINT_ACCEPTANCE_HOOK_DIR"/*; do
      [[ -f "$hook" && -x "$hook" ]] || return 1
      "$hook" "$release" "$sha" "$MAINT_BASE_URL" || return 1
    done
  fi
}

write_success_metadata() {
  local release="$1" sha="$2" temporary
  temporary="$MAINT_MARKER.tmp.$$"
  printf '%s\n' "$sha" | "${SUDO[@]}" tee "$temporary" >/dev/null
  "${SUDO[@]}" chmod 0644 "$temporary"
  "${SUDO[@]}" mv -T "$temporary" "$MAINT_MARKER"
  "${SUDO[@]}" cp "$release/PROVENANCE.txt" "$MAINT_ROOT/.PROVENANCE.tmp.$$"
  "${SUDO[@]}" mv -T "$MAINT_ROOT/.PROVENANCE.tmp.$$" "$MAINT_ROOT/PROVENANCE.txt"
}

activate() {
  local release="$1" sha="$2" production_before="$3"
  switch_venv "$release"
  if restart_maintenance_units && run_acceptance "$release" "$sha" "$production_before"; then
    write_success_metadata "$release" "$sha"
    return 0
  fi
  log "full acceptance failed; restoring previous maintenance venv"
  restore_previous
  restart_maintenance_units || fail "promotion and rollback restart both failed"
  fail "promotion failed full acceptance and was rolled back"
}

usage() {
  cat <<'EOF'
Usage:
  promote-omnigent-maintenance [production-commit-sha]
  promote-omnigent-maintenance --rollback <previous-commit-sha>

Promotion is intentionally a maintenance-window operation: it restarts only the
Omnigent 1 server/host units after verifying the exact O2-accepted artifact.
EOF
}

reject_web_ui_skip
require_root_or_sudo
preflight_tooling
mode=promote
sha=""
while (( $# )); do
  case "$1" in
    --rollback)
      mode=rollback; shift
      (( $# )) || refuse "--rollback requires an exact SHA"
      sha="$1"; shift
      ;;
    -h|--help)
      usage; exit 0
      ;;
    --*) refuse "unknown flag: $1" ;;
    *)
      [[ -z "$sha" ]] || refuse "unexpected positional: $1"
      sha="$1"; shift
      ;;
  esac
done

production_before=$(mktemp /tmp/omnigent-prod-before.XXXXXX)
trap 'rm -f "$production_before"' EXIT
capture_production_state "$production_before" || refuse "could not capture O2 baseline"

if [[ "$mode" == promote ]]; then
  [[ -f "$PROD_MARKER" ]] || refuse "O2 has no fully accepted maintenance-candidate marker"
  candidate=$(<"$PROD_MARKER")
  require_exact_sha "$candidate"
  if [[ -n "$sha" ]]; then
    require_exact_sha "$sha"
    [[ "$sha" == "$candidate" ]] || refuse "requested SHA is not the exact accepted O2 candidate"
  fi
  sha="$candidate"
  current_prod=$(readlink -f "$PROD_ROOT/current" 2>/dev/null || true)
  [[ "$current_prod" == "$PROD_ROOT/releases/$sha" ]] \
    || refuse "O2 current symlink does not match its promotion marker"
  RELEASE_DIR=""
  create_maintenance_release "$sha"
else
  require_exact_sha "$sha"
  RELEASE_DIR="$MAINT_ROOT/releases/$sha"
  [[ -f "$RELEASE_DIR/.complete" ]] && "$OMNIGENT_RELEASE_PREFLIGHT" release "$RELEASE_DIR" "$sha" >/dev/null \
    || refuse "rollback target failed release integrity checks"
fi

activate "$RELEASE_DIR" "$sha" "$production_before"
rm -f "$production_before"
trap - EXIT
if [[ "$mode" == rollback ]]; then
  printf 'maintenance rollback complete: %s\n' "$sha"
else
  printf 'maintenance promotion complete: %s\n' "$sha"
fi
