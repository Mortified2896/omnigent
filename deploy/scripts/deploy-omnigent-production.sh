#!/usr/bin/env bash
# deploy-omnigent-production — versioned deployment controller for Omnigent 2.
#
# Builds a new release, swaps /opt/omnigent-production/current atomically,
# and restarts only the Omnigent 2 systemd units. Rolls back automatically
# if the post-switch health check fails.
#
# Hard refusal: any reference to a maintenance path, port or service unit
# aborts the run. This script never touches the maintenance instance.
#
# Usage:
#   deploy-omnigent-production                            # deploy HEAD of the
#                                                       # bootstrap branch
#   deploy-omnigent-production <commit-sha>               # deploy a specific commit
#   deploy-omnigent-production --wheel <path> --no-build  # install pre-built wheel
#   deploy-omnigent-production --rollback [sha]           # manual rollback
#   deploy-omnigent-production --status                   # show current state
#
# Exit codes:
#   0  success
#   2  refused (safety guard)
#   3  build / verification failure
#   4  health check failed (rollback triggered)
#   5  bad usage

set -euo pipefail
shopt -s lastpipe

OMNIGENT_PROD_REPO="${OMNIGENT_PROD_REPO:-/home/hermes/workspace/repos/omnigent-production}"
OMNIGENT_PROD_BRANCH="${OMNIGENT_PROD_BRANCH:-bootstrap/omnigent-production-2}"
OMNIGENT_PROD_HOME="${OMNIGENT_PROD_HOME:-/var/lib/omnigent-production}"
OMNIGENT_PROD_RELEASE_ROOT="${OMNIGENT_PROD_RELEASE_ROOT:-/opt/omnigent-production}"
OMNIGENT_PROD_HEALTH_TIMEOUT_S="${OMNIGENT_PROD_HEALTH_TIMEOUT_S:-60}"

SERVER_HEALTH_URL="http://127.0.0.1:4197/health"
SERVER_UNIT=omnigent-production.service
HOST_UNIT=omnigent-production-host.service

# ── Safety guards ───────────────────────────────────────
MAINTENANCE_PATHS=( "/opt/omnigent" "/etc/omnigent" "/var/lib/omnigent" )
MAINTENANCE_SERVICES=( "omnigent.service" "omnigent-host.service" )
MAINTENANCE_PORTS=(4097 9461)

guard_log() { printf '[deploy-omnigent-production] %s\n' "$*" >&2; }
guard_die() { printf '[deploy-omnigent-production] REFUSED: %s\n' "$*" >&2; exit 2; }

check_no_maintenance_reference() {
  local what="$1" value="$2"
  [[ -n "$value" ]] || guard_die "empty value for $what"
  local p
  for p in "${MAINTENANCE_PATHS[@]}"; do
    if [[ "$value" == "$p" || "$value" == "$p/"* ]]; then
      guard_die "$what='$value' resolves into maintenance path '$p'"
    fi
  done
  local s
  for s in "${MAINTENANCE_SERVICES[@]}"; do
    if [[ "$value" == "$s" ]]; then
      guard_die "$what='$value' names maintenance service '$s'"
    fi
  done
  local port
  for port in "${MAINTENANCE_PORTS[@]}"; do
    if [[ "$value" == *":$port"* ]]; then
      guard_die "$what='$value' references maintenance port $port"
    fi
  done
}

for arg in "$@"; do check_no_maintenance_reference "arg" "$arg"; done
check_no_maintenance_reference "OMNIGENT_PROD_HOME" "$OMNIGENT_PROD_HOME"
check_no_maintenance_reference "OMNIGENT_PROD_RELEASE_ROOT" "$OMNIGENT_PROD_RELEASE_ROOT"
check_no_maintenance_reference "OMNIGENT_PROD_REPO" "$OMNIGENT_PROD_REPO"
check_no_maintenance_reference "SERVER_HEALTH_URL" "$SERVER_HEALTH_URL"

assert_isolated_paths() {
  if [[ -d /opt/omnigent && ! -d /opt/omnigent-production ]]; then
    guard_die "found /opt/omnigent but missing /opt/omnigent-production — refusing"
  fi
  if [[ -d /etc/omnigent && ! -d /etc/omnigent-production ]]; then
    guard_die "found /etc/omnigent but missing /etc/omnigent-production — refusing"
  fi
  if [[ -d /var/lib/omnigent && ! -d /var/lib/omnigent-production ]]; then
    guard_die "found /var/lib/omnigent but missing /var/lib/omnigent-production — refusing"
  fi
}

# ── Subcommands ─────────────────────────────────────────
print_status() {
  echo "=== current release ==="
  if [[ -L "$OMNIGENT_PROD_RELEASE_ROOT/current" ]]; then
    ls -la "$OMNIGENT_PROD_RELEASE_ROOT/current"
    readlink -f "$OMNIGENT_PROD_RELEASE_ROOT/current"
  else
    echo "(no current symlink)"
  fi
  echo
  echo "=== releases ==="
  ls -1 "$OMNIGENT_PROD_RELEASE_ROOT/releases/" 2>/dev/null | sort || echo "(none)"
  echo
  echo "=== health (production 4197) ==="
  curl -fsS --max-time 5 "$SERVER_HEALTH_URL" 2>&1 || echo "(unhealthy)"
  exit 0
}

require_root_or_sudo() {
  if [[ $EUID -eq 0 ]]; then SUDO=""; return 0; fi
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo -E"; return 0; fi
  guard_die "must run as root or with sudo available"
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || guard_die "missing required command: $1"; }
resolve_python312() {
  if [[ -x "/home/hermes/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12" ]]; then
    echo "/home/hermes/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12"
    return 0
  fi
  echo "3.12"
}

resolve_cmd() {
  local found
  found=$(command -v "$1" 2>/dev/null || true)
  if [[ -z "$found" && -x "$HOME/.local/bin/$1" ]]; then found="$HOME/.local/bin/$1"; fi
  if [[ -z "$found" && -x "/home/hermes/.local/bin/$1" ]]; then found="/home/hermes/.local/bin/$1"; fi
  [[ -n "$found" ]] || guard_die "missing required command: $1"
  readlink -f "$found"
}

require_repo() {
  [[ -d "$OMNIGENT_PROD_REPO/.git" ]] || guard_die "not a git repo: $OMNIGENT_PROD_REPO"
  (cd "$OMNIGENT_PROD_REPO" && git rev-parse --verify HEAD >/dev/null)
}

require_services_present() {
  $SUDO systemctl cat "$SERVER_UNIT" >/dev/null 2>&1 || guard_die "$SERVER_UNIT not installed"
  $SUDO systemctl cat "$HOST_UNIT" >/dev/null 2>&1 || guard_die "$HOST_UNIT not installed"
}

switch_symlink() {
  ln -sfn "$1" "$OMNIGENT_PROD_RELEASE_ROOT/.current.tmp"
  mv -T "$OMNIGENT_PROD_RELEASE_ROOT/.current.tmp" "$OMNIGENT_PROD_RELEASE_ROOT/current"
}

wait_healthy() {
  local deadline=$(( $(date +%s) + $1 ))
  while (( $(date +%s) < deadline )); do
    if curl -fsS --max-time 3 "$SERVER_HEALTH_URL" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

restart_units() {
  $SUDO systemctl restart "$SERVER_UNIT" || return 1
  wait_healthy "$OMNIGENT_PROD_HEALTH_TIMEOUT_S" || return 1
  $SUDO systemctl restart "$HOST_UNIT"
}

rollback() {
  local target="${1:-}"
  if [[ -z "$target" ]]; then
    target=$(ls -1t "$OMNIGENT_PROD_RELEASE_ROOT/releases/" | sed -n '2p' || true)
    [[ -n "$target" ]] || guard_die "no previous release to roll back to"
  fi
  local target_path="$OMNIGENT_PROD_RELEASE_ROOT/releases/$target"
  [[ -d "$target_path/venv" ]] || guard_die "rollback target missing venv: $target"
  guard_log "rolling back to $target"
  switch_symlink "$target_path"
  restart_units || guard_die "rollback restart failed"
  wait_healthy "$OMNIGENT_PROD_HEALTH_TIMEOUT_S" || guard_die "rollback health failed"
  echo "rollback complete: $target"
  exit 0
}

# ── Build + verify a release ───────────────────────────
build_release() {
  local sha="$1"
  local release_dir="$OMNIGENT_PROD_RELEASE_ROOT/releases/$sha"
  require_cmd git; require_repo
  local uv_path python_path
  uv_path=$(resolve_cmd uv)
  python_path=$(resolve_python312)
  if [[ -d "$release_dir" ]]; then
    if [[ -d "$release_dir/venv" && -f "$release_dir/.complete" ]] && \
       "$release_dir/venv/bin/python" -c "from omnigent.runtime import telemetry; assert callable(telemetry._fastapi_instrumentation_enabled)" >/dev/null 2>&1; then
      echo "$release_dir"
      return 0
    fi
    guard_log "removing incomplete release: $release_dir"
    $SUDO rm -rf "$release_dir"
  fi
  (cd "$OMNIGENT_PROD_REPO" && git rev-parse --verify "$sha" >/dev/null) || guard_die "unknown commit $sha"
  local build_dir
  build_dir=$(mktemp -d /tmp/omnigent-prod-build.XXXXXX)
  trap '[[ -n "${build_dir:-}" ]] && rm -rf "$build_dir"' EXIT
  (cd "$OMNIGENT_PROD_REPO" && git worktree add --detach "$build_dir" "$sha" >/dev/null)
  $SUDO mkdir -p "$release_dir"
  $SUDO "$uv_path" venv --python "$python_path" "$release_dir/venv"
  (cd "$build_dir" && "$uv_path" build --out-dir "$build_dir/dist")
  local wheels=()
  shopt -s nullglob
  wheels=("$build_dir/dist"/omnigent-*.whl)
  shopt -u nullglob
  (( ${#wheels[@]} == 1 )) || guard_die "expected exactly one built wheel, found ${#wheels[@]}"
  local wheel_path
  wheel_path=$(readlink -f "${wheels[0]}")
  $SUDO "$uv_path" pip install --python "$release_dir/venv/bin/python" "${wheel_path}[all]"
  {
    echo "sha=$sha"
    echo "package_version=$("$release_dir/venv/bin/python" -c "from omnigent.version import VERSION; print(VERSION)")"
    echo "wheel_sha256=$(sha256sum "$wheel_path" | awk '{print $1}')"
    echo "built_at_utc=$(date -u +%FT%TZ)"
    echo "builder=$(whoami)@$(hostname)"
  } | $SUDO tee "$release_dir/PROVENANCE.txt" >/dev/null
  if ! "$release_dir/venv/bin/python" -c \
      "from omnigent.runtime import telemetry; assert callable(telemetry._fastapi_instrumentation_enabled)"; then
    $SUDO rm -rf "$release_dir"
    guard_die "release verification failed: missing _fastapi_instrumentation_enabled"
  fi
  $SUDO touch "$release_dir/.complete"
  echo "$release_dir"
}

# ── Deploy flow ─────────────────────────────────────────
deploy() {
  local sha="$1" wheel_arg="$2" no_build="$3"
  require_services_present
  local release_dir=""
  if [[ -n "$wheel_arg" ]]; then
    [[ "$no_build" == "true" ]] || guard_die "--wheel requires --no-build"
    [[ -f "$wheel_arg" ]] || guard_die "wheel not found: $wheel_arg"
    sha="wheel-$(sha256sum "$wheel_arg" | awk '{print substr($1,1,16)}')"
    release_dir="$OMNIGENT_PROD_RELEASE_ROOT/releases/$sha"
    local uv_path python_path wheel_path
    uv_path=$(resolve_cmd uv)
    python_path=$(resolve_python312)
    wheel_path=$(readlink -f "$wheel_arg")
    if [[ -d "$release_dir" ]]; then
      guard_log "removing existing wheel release before install: $release_dir"
      $SUDO rm -rf "$release_dir"
    fi
    $SUDO mkdir -p "$release_dir"
    $SUDO "$uv_path" venv --python "$python_path" "$release_dir/venv"
    $SUDO "$uv_path" pip install --python "$release_dir/venv/bin/python" "${wheel_path}[all]"
    $SUDO touch "$release_dir/.complete"
  else
    release_dir=$(build_release "$sha")
    sha=$(basename "$release_dir")
  fi
  guard_log "swapping symlink to $sha"
  local previous_link=""
  [[ -L "$OMNIGENT_PROD_RELEASE_ROOT/current" ]] && \
    previous_link=$(readlink -f "$OMNIGENT_PROD_RELEASE_ROOT/current")
  switch_symlink "$release_dir"
  if ! restart_units; then
    guard_log "post-switch health check failed; rolling back"
    [[ -n "$previous_link" ]] && { switch_symlink "$previous_link"; restart_units || guard_die "rollback also failed"; }
    guard_die "deploy failed; rolled back"
  fi
  echo "deploy complete: $sha"
}

# ── Argument parsing ────────────────────────────────────
SUDO=""
require_root_or_sudo
assert_isolated_paths

mode="deploy"
sha=""
wheel=""
no_build="false"

while (( $# > 0 )); do
  case "$1" in
    --status) print_status ;;
    --rollback) mode="rollback"; shift; sha="${1:-}" ;;
    --wheel) wheel="$2"; shift 2 ;;
    --no-build) no_build="true"; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --*) guard_die "unknown flag: $1" ;;
    *)
      [[ -z "$sha" ]] || guard_die "unexpected positional: $1"
      sha="$1"; shift ;;
  esac
done

case "$mode" in
  rollback) rollback "${sha:-}" ;;
  deploy)
    if [[ -z "$sha" && -z "$wheel" ]]; then
      require_repo
      sha=$(cd "$OMNIGENT_PROD_REPO" && git rev-parse "$OMNIGENT_PROD_BRANCH")
    fi
    deploy "${sha:-}" "${wheel:-}" "$no_build"
    ;;
esac
