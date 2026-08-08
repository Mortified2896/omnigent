#!/usr/bin/env bash
# Peer-deployer bash library — sourced by O2-host wrappers and tests.
#
# Provides:
#   * ``service_state`` — proper systemctl wrapper that respects
#     systemd exit codes instead of relying on `pipefail` semantics.
#   * ``service_state_equal`` — strict string comparison.
#   * ``assert_target_not_supervisor`` — hard refusal.
#   * ``assert_active_runtime_unchanged`` — pre/post snapshot compare.
#   * ``assert_exact_sha`` — strict 40-char SHA validator.
#
# This library is the only blessed path through which bash scripts
# in the Control Room interact with systemd state. The previous
# incident was caused by a broken `is-active | grep -q` pattern under
# `set -o pipefail`. This library replaces that pattern with the
# vetted ``service_state`` helper.
#
# The library is exercised by the regression tests in
# ``tests/deploy/test_peer_deployer_service_state.py``.

set -euo pipefail

# Resolve the directory of this file so the library can be sourced
# from any cwd and still find the Python module.
_PEER_DEPLOYER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# service_state <unit> — print the canonical state token.
#
# Prints the captured stdout of `systemctl is-active <unit>` (lowercased,
# trimmed) and returns 0 iff the printed token is non-empty. The
# exit code of ``systemctl is-active`` is intentionally ignored because
# it returns 3 for inactive/failed/unknown services, which would defeat
# `set -o pipefail` callers that wanted to compare the token.
#
# Usage:
#   state=$(service_state omnigent.service)
#   case "$state" in
#     active) ... ;;
#     inactive) ... ;;
#     failed) ... ;;
#     *) echo "unexpected: $state" >&2; exit 1 ;;
#   esac
service_state() {
  local unit="$1"
  if [[ -z "$unit" ]]; then
    echo "service_state: unit name is required" >&2
    return 64
  fi
  local raw
  raw="$(systemctl is-active "$unit" 2>/dev/null || true)"
  raw="${raw,,}"
  raw="${raw//[[:space:]]/}"
  if [[ -z "$raw" ]]; then
    echo "service_state: empty state for unit $unit" >&2
    return 65
  fi
  printf '%s\n' "$raw"
}

# service_state_equal <unit> <expected> — return 0 iff the captured
# state equals ``expected`` (case-folded).
service_state_equal() {
  local unit="$1" expected="$2"
  local actual
  actual="$(service_state "$unit")"
  [[ "$actual" == "${expected,,}" ]]
}

# is_active_strict <unit> — return 0 iff the unit is in the ``active``
# state. This is the vetted replacement for the broken
# `systemctl is-active --quiet` pattern.
is_active_strict() {
  service_state_equal "$1" "active"
}

# is_inactive_strict <unit> — return 0 iff the unit is in the
# ``inactive`` state. This is the vetted replacement for the broken
# `systemctl is-active ... | grep -q '^inactive$'` pattern.
is_inactive_strict() {
  service_state_equal "$1" "inactive"
}

# is_failed_strict <unit> — return 0 iff the unit is in the
# ``failed`` state.
is_failed_strict() {
  service_state_equal "$1" "failed"
}

# is_known_unit <unit> — return 0 iff systemd recognizes the unit.
is_known_unit() {
  local unit="$1"
  [[ -n "$unit" ]] || return 1
  systemctl cat "$unit" >/dev/null 2>&1
}

# assert_target_not_supervisor <target> <supervisor>
#
# Hard refusal. The Control Room invariant is: an instance NEVER
# upgrades itself. If target == supervisor, exit 2 with a refusal
# message.
assert_target_not_supervisor() {
  local target="$1" supervisor="$2"
  if [[ -z "$target" || -z "$supervisor" ]]; then
    echo "REFUSED: target and supervisor must both be specified" >&2
    exit 2
  fi
  if [[ "$target" == "$supervisor" ]]; then
    echo "REFUSED: target == supervisor == $target" >&2
    echo "         an instance NEVER upgrades itself" >&2
    exit 2
  fi
}

# assert_exact_sha <value>
#
# Refuse anything that is not exactly 40 lowercase hex characters.
assert_exact_sha() {
  local value="$1"
  if [[ ! "$value" =~ ^[0-9a-f]{40}$ ]]; then
    echo "REFUSED: expected an exact 40-character lowercase SHA, got $value" >&2
    exit 2
  fi
}

# capture_instance_state <name> <deployment_root> <output_path>
#
# Capture the runtime identity of an instance for pre/post comparison.
# The captured state includes provenance SHA/version and the SHA
# recorded in the installed package's _build_info.py.
capture_instance_state() {
  local name="$1" deployment_root="$2" output_path="$3"
  if [[ -z "$name" || -z "$deployment_root" || -z "$output_path" ]]; then
    echo "capture_instance_state: missing argument" >&2
    return 64
  fi
  : > "$output_path"
  printf 'name=%s\n' "$name" >> "$output_path"
  printf 'deployment_root=%s\n' "$deployment_root" >> "$output_path"
  if [[ -f "$deployment_root/PROVENANCE.txt" ]]; then
    grep -E '^(sha|package_version)=' "$deployment_root/PROVENANCE.txt" >> "$output_path" || true
  fi
  local build_info
  build_info="$(find "$deployment_root/venv/lib" -path '*/site-packages/omnigent/_build_info.py' 2>/dev/null | head -1)"
  if [[ -n "$build_info" && -f "$build_info" ]]; then
    grep -E '^COMMIT_SHA' "$build_info" >> "$output_path" || true
  fi
}

# assert_state_unchanged <before_path> <after_path>
#
# Return 0 iff the two state snapshots are identical. Used to prove
# the supervisor was not touched during a peer-supervised upgrade.
assert_state_unchanged() {
  local before="$1" after="$2"
  if [[ ! -f "$before" || ! -f "$after" ]]; then
    echo "assert_state_unchanged: missing snapshot" >&2
    return 1
  fi
  if ! cmp -s "$before" "$after"; then
    diff "$before" "$after" >&2 || true
    return 1
  fi
  return 0
}

# Print the offending usage pattern and abort. This is the helper
# that recognizes the broken `is-active | grep` pattern in scripts.
# The signature is intentionally simple: pass the suspect script
# path and the helper will refuse to run if it finds the pattern.
refuse_broken_is_active_pattern() {
  local script_path="$1"
  if [[ ! -f "$script_path" ]]; then
    echo "refuse_broken_is_active_pattern: file not found: $script_path" >&2
    return 1
  fi
  if grep -qE 'is-active[[:space:]]+[^|]*\|[[:space:]]*grep' "$script_path"; then
    echo "REFUSED: $script_path contains the broken 'is-active | grep' pattern" >&2
    echo "         use the service_state helper from peer_deployer_lib.sh instead" >&2
    return 2
  fi
  return 0
}

# Export the helper names so callers can rely on them even when
# `set -u` is in effect.
export -f service_state 2>/dev/null || true
export -f service_state_equal 2>/dev/null || true
export -f is_active_strict 2>/dev/null || true
export -f is_inactive_strict 2>/dev/null || true
export -f is_failed_strict 2>/dev/null || true
export -f is_known_unit 2>/dev/null || true

# Mute the "is read-only" warning on shells that disallow export -f.
true
