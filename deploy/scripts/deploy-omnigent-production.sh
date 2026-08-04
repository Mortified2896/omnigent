#!/usr/bin/env bash
# Versioned, rollback-safe deployment controller for Omnigent production.
set -euo pipefail
shopt -s nullglob

OMNIGENT_PROD_REPO="${OMNIGENT_PROD_REPO:-/home/hermes/workspace/repos/omnigent-production}"
OMNIGENT_PROD_BRANCH="${OMNIGENT_PROD_BRANCH:-origin/main}"
OMNIGENT_PROD_HOME="${OMNIGENT_PROD_HOME:-/var/lib/omnigent-production}"
OMNIGENT_PROD_RELEASE_ROOT="${OMNIGENT_PROD_RELEASE_ROOT:-/opt/omnigent-production}"
OMNIGENT_PROD_HEALTH_TIMEOUT_S="${OMNIGENT_PROD_HEALTH_TIMEOUT_S:-60}"
OMNIGENT_PROD_ACCEPTANCE_HOOK_DIR="${OMNIGENT_PROD_ACCEPTANCE_HOOK_DIR:-}"
OMNIGENT_RELEASE_PREFLIGHT="${OMNIGENT_RELEASE_PREFLIGHT:-$OMNIGENT_PROD_REPO/deploy/scripts/omnigent_release_preflight.py}"
SERVER_HEALTH_URL="${OMNIGENT_PROD_HEALTH_URL:-http://127.0.0.1:4197/health}"
SERVER_BASE_URL="${SERVER_HEALTH_URL%/health}"
SERVER_UNIT=omnigent-production.service
HOST_UNIT=omnigent-production-host.service
PROMOTION_MARKER="$OMNIGENT_PROD_RELEASE_ROOT/maintenance-candidate.sha"
MAINTENANCE_PATHS=(/opt/omnigent /etc/omnigent /var/lib/omnigent)
MAINTENANCE_SERVICES=(omnigent.service omnigent-host.service)
MAINTENANCE_PORTS=(4097 9461)
SUDO=()

log() { printf '[deploy-omnigent-production] %s\n' "$*" >&2; }
refuse() { printf '[deploy-omnigent-production] REFUSED: %s\n' "$*" >&2; exit 2; }
fail_build() { printf '[deploy-omnigent-production] FAILED: %s\n' "$*" >&2; exit 3; }
fail_acceptance() { printf '[deploy-omnigent-production] FAILED: %s\n' "$*" >&2; exit 4; }

reject_web_ui_skip() {
  [[ ! ${OMNIGENT_SKIP_WEB_UI+x} ]] || refuse "OMNIGENT_SKIP_WEB_UI must be unset for production deploys"
  [[ ! ${OMNIAGENTS_SKIP_WEB_UI+x} ]] || refuse "OMNIAGENTS_SKIP_WEB_UI must be unset (legacy alias for OMNIGENT_SKIP_WEB_UI)"
}

check_no_maintenance_reference() {
  local what="$1" value="$2" item
  [[ -n "$value" ]] || refuse "empty value for $what"
  for item in "${MAINTENANCE_PATHS[@]}"; do
    [[ "$value" != "$item" && "$value" != "$item/"* ]] || refuse "$what='$value' resolves into maintenance path '$item'"
  done
  for item in "${MAINTENANCE_SERVICES[@]}"; do
    [[ "$value" != "$item" ]] || refuse "$what='$value' names maintenance service '$item'"
  done
  for item in "${MAINTENANCE_PORTS[@]}"; do
    [[ "$value" != *":$item"* ]] || refuse "$what='$value' references maintenance port $item"
  done
}

require_root_or_sudo() {
  if [[ $EUID -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || refuse "must run as root or with sudo available"
    SUDO=(sudo -E)
  fi
}
require_cmd() { command -v "$1" >/dev/null 2>&1 || refuse "missing required command: $1"; }
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
require_repo() {
  [[ -d "$OMNIGENT_PROD_REPO/.git" ]] || refuse "not a git repo: $OMNIGENT_PROD_REPO"
  git -C "$OMNIGENT_PROD_REPO" rev-parse --verify HEAD >/dev/null || refuse "invalid git repository"
}
require_services_present() {
  "${SUDO[@]}" systemctl cat "$SERVER_UNIT" >/dev/null 2>&1 || refuse "$SERVER_UNIT not installed"
  "${SUDO[@]}" systemctl cat "$HOST_UNIT" >/dev/null 2>&1 || refuse "$HOST_UNIT not installed"
}
preflight_tooling() {
  local command uv_path python_path node_major pnpm_path
  for command in git curl sha256sum mktemp mv ln date cmp python3 systemctl; do require_cmd "$command"; done
  [[ -x "$OMNIGENT_RELEASE_PREFLIGHT" ]] || refuse "missing executable preflight: $OMNIGENT_RELEASE_PREFLIGHT"
  uv_path=$(resolve_cmd uv)
  python_path=$(resolve_python312)
  "$python_path" -c 'import sys; assert sys.version_info[:2] == (3, 12)' || refuse "Python 3.12 check failed"
  printf '%s\n%s\n' "$uv_path" "$python_path"
  if [[ "${1:-}" == build ]]; then
    require_cmd node
    node_major=$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null) || refuse "could not determine Node.js version"
    (( node_major >= 22 )) || refuse "Node.js 22 or newer is required"
    pnpm_path=$(command -v pnpm 2>/dev/null || command -v corepack 2>/dev/null || true)
    [[ -n "$pnpm_path" ]] || refuse "pnpm or corepack is required to build the SPA"
  fi
}

assert_isolated_paths() {
  [[ ! -d /opt/omnigent || -d /opt/omnigent-production ]] || refuse "maintenance exists but production release root is missing"
  [[ ! -d /etc/omnigent || -d /etc/omnigent-production ]] || refuse "maintenance exists but production config root is missing"
  [[ ! -d /var/lib/omnigent || -d /var/lib/omnigent-production ]] || refuse "maintenance exists but production state root is missing"
}
canonical_sha() {
  local resolved
  resolved=$(git -C "$OMNIGENT_PROD_REPO" rev-parse --verify "$1^{commit}" 2>/dev/null) || refuse "unknown commit $1"
  [[ "$resolved" =~ ^[0-9a-f]{40}$ ]] || refuse "git returned a non-canonical SHA"
  printf '%s\n' "$resolved"
}

switch_symlink() {
  local target="$1"
  "${SUDO[@]}" ln -sfn "$target" "$OMNIGENT_PROD_RELEASE_ROOT/.current.tmp"
  "${SUDO[@]}" mv -T "$OMNIGENT_PROD_RELEASE_ROOT/.current.tmp" "$OMNIGENT_PROD_RELEASE_ROOT/current"
}
write_marker() {
  local sha="$1" temporary="$PROMOTION_MARKER.tmp.$$"
  printf '%s\n' "$sha" | "${SUDO[@]}" tee "$temporary" >/dev/null
  "${SUDO[@]}" chmod 0644 "$temporary"
  "${SUDO[@]}" mv -T "$temporary" "$PROMOTION_MARKER"
}
wait_healthy() {
  local deadline=$(( $(date +%s) + $1 ))
  while (( $(date +%s) < deadline )); do
    curl -fsS --max-time 3 "$SERVER_HEALTH_URL" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
restart_units() {
  "${SUDO[@]}" systemctl restart "$SERVER_UNIT" || return 1
  wait_healthy "$OMNIGENT_PROD_HEALTH_TIMEOUT_S" || return 1
  "${SUDO[@]}" systemctl restart "$HOST_UNIT" || return 1
}
capture_maintenance_state() {
  local output="$1"
  : > "$output"
  local unit
  for unit in "${MAINTENANCE_SERVICES[@]}"; do
    systemctl show "$unit" -p MainPID -p ActiveEnterTimestampMonotonic >> "$output" || return 1
  done
}

run_acceptance() {
  local release_dir="$1" sha="$2" maintenance_before="$3"
  local body info installed_index hook
  "$OMNIGENT_RELEASE_PREFLIGHT" release "$release_dir" "$sha" >/dev/null || return 1
  systemctl is-active --quiet "$SERVER_UNIT" || return 1
  systemctl is-active --quiet "$HOST_UNIT" || return 1
  wait_healthy "$OMNIGENT_PROD_HEALTH_TIMEOUT_S" || return 1
  body=$(mktemp /tmp/omnigent-prod-root.XXXXXX)
  info=$(mktemp /tmp/omnigent-prod-info.XXXXXX)
  trap 'rm -f "${body:-}" "${info:-}"' RETURN
  curl -fsS --max-time 10 "$SERVER_BASE_URL/" > "$body" || return 1
  installed_index=$("$release_dir/venv/bin/python" -c 'from pathlib import Path; import omnigent; print(Path(omnigent.__file__).parent / "server/static/web-ui/index.html")') || return 1
  cmp -s "$installed_index" "$body" || { log "runtime root does not match the installed SPA index"; return 1; }
  curl -fsS --max-time 10 "$SERVER_BASE_URL/v1/info" > "$info" || return 1
  python3 - "$info" "$release_dir/PROVENANCE.txt" <<'PY' || return 1
import json, sys
info = json.load(open(sys.argv[1]))
provenance = dict(line.rstrip().split("=", 1) for line in open(sys.argv[2]) if "=" in line)
assert info["server_version"] == provenance["package_version"]
PY
  local maintenance_after
  maintenance_after=$(mktemp /tmp/omnigent-maint-after.XXXXXX)
  capture_maintenance_state "$maintenance_after" || { rm -f "$maintenance_after"; return 1; }
  cmp -s "$maintenance_before" "$maintenance_after" || { log "maintenance service state changed during production deploy"; rm -f "$maintenance_after"; return 1; }
  rm -f "$maintenance_after"
  if [[ -n "$OMNIGENT_PROD_ACCEPTANCE_HOOK_DIR" ]]; then
    [[ -d "$OMNIGENT_PROD_ACCEPTANCE_HOOK_DIR" ]] || { log "acceptance hook directory is missing"; return 1; }
    for hook in "$OMNIGENT_PROD_ACCEPTANCE_HOOK_DIR"/*; do
      [[ -f "$hook" && -x "$hook" ]] || { log "acceptance hook is not an executable file: $hook"; return 1; }
      "$hook" "$release_dir" "$sha" "$SERVER_BASE_URL" || return 1
    done
  fi
  rm -f "$body" "$info"
  trap - RETURN
}

create_release() {
  local sha="$1" supplied_wheel="$2" build_mode="$3"
  local final="$OMNIGENT_PROD_RELEASE_ROOT/releases/$sha"
  if [[ -e "$final" ]]; then
    [[ -f "$final/.complete" ]] && "$OMNIGENT_RELEASE_PREFLIGHT" release "$final" "$sha" >/dev/null \
      || refuse "existing release $final failed integrity checks; refusing unsafe reuse"
    RELEASE_DIR="$final"
    log "reusing fully verified immutable release $sha"
    return
  fi

  local uv_path python_path staging_root staging build_dir="" wheel_path wheel_name package_version wheel_hash
  mapfile -t tools < <(preflight_tooling "$build_mode")
  uv_path="${tools[0]}"; python_path="${tools[1]}"
  staging_root="$OMNIGENT_PROD_RELEASE_ROOT/releases/.staging-$sha-$$"
  staging="$staging_root/$sha"
  [[ ! -e "$staging_root" ]] || refuse "staging path already exists: $staging_root"
  "${SUDO[@]}" mkdir -p "$staging/artifacts"
  cleanup_candidate() {
    [[ -z "$build_dir" ]] || git -C "$OMNIGENT_PROD_REPO" worktree remove --force "$build_dir" >/dev/null 2>&1 || true
    "${SUDO[@]}" rm -rf "$staging_root"
  }
  trap cleanup_candidate EXIT

  if [[ "$build_mode" == build ]]; then
    build_dir=$(mktemp -d /tmp/omnigent-prod-build.XXXXXX)
    git -C "$OMNIGENT_PROD_REPO" worktree add --detach "$build_dir" "$sha" >/dev/null || fail_build "git worktree creation failed"
    (cd "$build_dir" && OMNIGENT_BUILD_WEB_UI=1 "$uv_path" build --wheel --out-dir "$build_dir/dist") || fail_build "wheel build failed"
    local wheels=("$build_dir"/dist/omnigent-*.whl)
    (( ${#wheels[@]} == 1 )) || fail_build "expected exactly one built wheel, found ${#wheels[@]}"
    wheel_path="${wheels[0]}"
  else
    wheel_path=$(readlink -f "$supplied_wheel")
  fi
  "$OMNIGENT_RELEASE_PREFLIGHT" wheel "$wheel_path" "$sha" >/dev/null || fail_build "wheel preflight failed"
  wheel_name=$(basename "$wheel_path")
  wheel_hash=$(sha256sum "$wheel_path" | awk '{print $1}')
  "${SUDO[@]}" cp "$wheel_path" "$staging/artifacts/$wheel_name"
  "${SUDO[@]}" "$uv_path" venv --python "$python_path" "$staging/venv" || fail_build "venv creation failed"
  "${SUDO[@]}" "$uv_path" pip install --python "$staging/venv/bin/python" "$staging/artifacts/$wheel_name[all]" || fail_build "wheel install failed"
  package_version=$("$staging/venv/bin/python" -c 'from omnigent.version import VERSION; print(VERSION)') || fail_build "installed version check failed"
  {
    printf 'schema_version=1\nsha=%s\npackage_version=%s\n' "$sha" "$package_version"
    printf 'wheel_sha256=%s\nwheel_filename=%s\n' "$wheel_hash" "$wheel_name"
    printf 'built_at_utc=%s\nbuilder=%s@%s\n' "$(date -u +%FT%TZ)" "$(whoami)" "$(hostname)"
  } | "${SUDO[@]}" tee "$staging/PROVENANCE.txt" >/dev/null
  "$OMNIGENT_RELEASE_PREFLIGHT" release "$staging" "$sha" --wheel-sha "$wheel_hash" >/dev/null || fail_build "installed release preflight failed"
  "${SUDO[@]}" touch "$staging/.complete"
  "${SUDO[@]}" chmod a-w "$staging/PROVENANCE.txt" "$staging/artifacts/$wheel_name"
  "${SUDO[@]}" mv -T "$staging" "$final"
  "${SUDO[@]}" rmdir "$staging_root"
  [[ -z "$build_dir" ]] || git -C "$OMNIGENT_PROD_REPO" worktree remove --force "$build_dir" >/dev/null
  build_dir=""
  trap - EXIT
  RELEASE_DIR="$final"
}

activate_release() {
  local release_dir="$1" sha="$2" previous="$3" maintenance_before="$4"
  log "switching production to $sha"
  switch_symlink "$release_dir"
  if restart_units && run_acceptance "$release_dir" "$sha" "$maintenance_before"; then
    write_marker "$sha"
    log "full acceptance passed; maintenance candidate is exactly $sha"
    return 0
  fi
  log "post-switch acceptance failed; rolling back"
  if [[ -n "$previous" ]]; then
    switch_symlink "$previous"
    restart_units || fail_acceptance "target failed and rollback restart also failed"
  fi
  fail_acceptance "target failed full acceptance and was rolled back"
}

manual_rollback() {
  local target="${1:-}" current candidate maintenance_before
  current=$(readlink -f "$OMNIGENT_PROD_RELEASE_ROOT/current" 2>/dev/null || true)
  if [[ -z "$target" ]]; then
    for candidate in "$OMNIGENT_PROD_RELEASE_ROOT"/releases/*; do
      [[ -d "$candidate" && "$candidate" != "$current" ]] || continue
      target=$(basename "$candidate"); break
    done
    [[ -n "$target" ]] || refuse "no previous release is available"
  fi
  [[ "$target" =~ ^[0-9a-f]{40}$ ]] || refuse "rollback target must be an exact commit SHA"
  local release_dir="$OMNIGENT_PROD_RELEASE_ROOT/releases/$target"
  [[ -f "$release_dir/.complete" ]] && "$OMNIGENT_RELEASE_PREFLIGHT" release "$release_dir" "$target" >/dev/null \
    || refuse "rollback target failed release integrity checks"
  maintenance_before=$(mktemp /tmp/omnigent-maint-before.XXXXXX)
  trap 'rm -f "$maintenance_before"' EXIT
  capture_maintenance_state "$maintenance_before" || refuse "could not capture maintenance baseline"
  activate_release "$release_dir" "$target" "$current" "$maintenance_before"
  rm -f "$maintenance_before"; trap - EXIT
  printf 'rollback complete: %s\n' "$target"
}

print_status() {
  printf 'current='; readlink -f "$OMNIGENT_PROD_RELEASE_ROOT/current" 2>/dev/null || printf '(none)\n'
  printf 'maintenance_candidate='; cat "$PROMOTION_MARKER" 2>/dev/null || printf '(none)\n'
  "$OMNIGENT_RELEASE_PREFLIGHT" release "$(readlink -f "$OMNIGENT_PROD_RELEASE_ROOT/current")" "$(basename "$(readlink -f "$OMNIGENT_PROD_RELEASE_ROOT/current")")" 2>/dev/null \
    && printf 'integrity=ok\n' || printf 'integrity=FAILED\n'
}
usage() {
  cat <<'EOF'
Usage:
  deploy-omnigent-production [commit-sha]
  deploy-omnigent-production <commit-sha> --wheel <path> --no-build
  deploy-omnigent-production --rollback [commit-sha]
  deploy-omnigent-production --status
EOF
}

reject_web_ui_skip
for arg in "$@"; do check_no_maintenance_reference arg "$arg"; done
check_no_maintenance_reference OMNIGENT_PROD_HOME "$OMNIGENT_PROD_HOME"
check_no_maintenance_reference OMNIGENT_PROD_RELEASE_ROOT "$OMNIGENT_PROD_RELEASE_ROOT"
check_no_maintenance_reference OMNIGENT_PROD_REPO "$OMNIGENT_PROD_REPO"
check_no_maintenance_reference SERVER_HEALTH_URL "$SERVER_HEALTH_URL"
require_root_or_sudo
assert_isolated_paths

mode=deploy sha="" wheel="" no_build=false
while (( $# )); do
  case "$1" in
    --status) mode=status; shift ;;
    --rollback) mode=rollback; shift; [[ $# -eq 0 || "$1" == --* ]] || { sha="$1"; shift; } ;;
    --wheel) (( $# >= 2 )) || refuse "--wheel requires a path"; wheel="$2"; shift 2 ;;
    --no-build) no_build=true; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) refuse "unknown flag: $1" ;;
    *) [[ -z "$sha" ]] || refuse "unexpected positional: $1"; sha="$1"; shift ;;
  esac
done

case "$mode" in
  status) print_status ;;
  rollback)
    require_services_present; preflight_tooling reuse >/dev/null; manual_rollback "$sha"
    ;;
  deploy)
    require_repo; require_services_present
    if [[ -n "$wheel" ]]; then
      [[ "$no_build" == true ]] || refuse "--wheel requires --no-build"
      [[ -n "$sha" ]] || refuse "a prebuilt wheel requires its exact commit SHA"
      [[ -f "$wheel" ]] || refuse "wheel not found: $wheel"
    else
      [[ "$no_build" == false ]] || refuse "--no-build requires --wheel"
      [[ -n "$sha" ]] || sha="$OMNIGENT_PROD_BRANCH"
    fi
    sha=$(canonical_sha "$sha")
    maintenance_before=$(mktemp /tmp/omnigent-maint-before.XXXXXX)
    trap 'rm -f "$maintenance_before"' EXIT
    capture_maintenance_state "$maintenance_before" || refuse "could not capture maintenance baseline"
    RELEASE_DIR=""
    create_release "$sha" "$wheel" "$([[ -n "$wheel" ]] && printf reuse || printf build)"
    previous=$(readlink -f "$OMNIGENT_PROD_RELEASE_ROOT/current" 2>/dev/null || true)
    activate_release "$RELEASE_DIR" "$sha" "$previous" "$maintenance_before"
    rm -f "$maintenance_before"; trap - EXIT
    printf 'deploy complete: %s\n' "$sha"
    ;;
esac
