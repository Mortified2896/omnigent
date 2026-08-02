#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# canary.sh — Phase D disposable-host canary runner
# ─────────────────────────────────────────────────────────────────────
#
# Note: this runner is intentionally /usr/bin/env bash (not POSIX
# sh) because it uses bash-only features: associative arrays
# (declare -A), process substitution, and local variables in
# functions. The per-check scripts (checks/*.sh) are POSIX sh.
#
# This script orchestrates the per-check scripts; it does NOT
# itself issue network requests or read the database. The checks
# do that.
#
# Runs the 12 acceptance checks against a fresh install of the
# rebuild's Omnigent 0.7 wheel, against an empty data dir, on a
# disposable host. Aggregates per-check outcomes into
# docs/rebuild/canary-report.md.
#
# Usage:
#   /opt/canary-fixtures/canary.sh run [--rebuild-sha <sha>]
#
# Exit codes:
#   0  all 12 checks PASS (or the harness-binary-missing ones
#      SKIPPED); canary is green.
#   1  at least one check FAIL; canary is red.

set -eu

CANARY_ROOT="$(cd "$(dirname "$0")" && pwd)"
CHECKS_DIR="$CANARY_ROOT/checks"

# Required environment (set by the operator or the canary's
# systemd / cron wrapper).
: "${OMNIGENT_PORT:=6767}"
: "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
: "${OMNIGENT_DATA_DIR:=/var/lib/canary-omnigent}"
: "${CANARY_IDENTITY:=canary@omnigent.local}"
: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"
: "${UPSTREAM_HEAD_SHA:=zf1a2b3c4d5e}"
: "${UNIT_NAME:=omnigent}"
: "${PROD_PORT:=$OMNIGENT_PORT}"
: "${PROD_HOSTNAME:=}"
: "${REBUILD_SHA:=}"
: "${LANGFUSE_HOST:=}"
: "${LANGFUSE_PUBLIC_KEY:=}"
: "${LANGFUSE_SECRET_KEY:=}"
: "${OMNIROUTE_BASE_URL:=}"
: "${OMNIROUTE_AUTH_TOKEN:=}"
: "${OMNIROUTE_ROUTER_NAME:=omniroute}"
: "${OMNIROUTE_API_KEY:=}"
: "${REPORT_PATH:=$CANARY_ROOT/canary-report.md}"

mkdir -p "$(dirname "$REPORT_PATH")"
mkdir -p "$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID"

export OMNIGENT_PORT OMNIGENT_AUTH_HEADER OMNIGENT_DATA_DIR
export CANARY_IDENTITY CANARY_FIXTURES_ROOT CANARY_RUN_ID
export UPSTREAM_HEAD_SHA UNIT_NAME PROD_PORT PROD_HOSTNAME
export LANGFUSE_HOST LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
export OMNIROUTE_BASE_URL OMNIROUTE_AUTH_TOKEN OMNIROUTE_ROUTER_NAME OMNIROUTE_API_KEY

# Per-check status tracking.
declare -A STATUS DURATION EVIDENCE
RESULTS_TSV="$CANARY_ROOT/.canary-results.$CANARY_RUN_ID.tsv"
: >"$RESULTS_TSV"

run_check() {
  local name="$1"
  local script="$2"
  local start end elapsed status_line rest
  start=$(date +%s)
  case "$script" in
    *.sh)
      if [ -e "$script" ]; then
        status_line=$(sh "$script" 2>&1) || true
      else
        status_line="FAIL script not found: $script"
      fi
      ;;
    *.py)
      if [ -e "$script" ]; then
        status_line=$(python3 "$script" 2>&1) || true
      else
        status_line="FAIL script not found: $script"
      fi
      ;;
    *)
      status_line="FAIL unknown check script extension: $script"
      ;;
  esac
  end=$(date +%s)
  elapsed=$((end - start))

  # Extract the status. Two formats:
  #   shell checks: first line is "PASS"|"FAIL"|"SKIPPED", rest is
  #                 free-form evidence (multi-line).
  #   python checks: single-line JSON object whose `status` key is
  #                 "PASS"|"FAIL"|"SKIPPED", and the rest of the keys
  #                 are evidence.
  first_line=$(printf '%s\n' "$status_line" | head -1)
  case "$first_line" in
    PASS|FAIL|SKIPPED)
      status="$first_line"
      rest=$(printf '%s\n' "$status_line" | tail -n +2)
      ;;
    \{*)
      # Try to parse the first line as JSON; if it has a `status`
      # key, use it.
      status=$(printf '%s' "$first_line" | python3 -c "
import json, sys
try:
    print(json.loads(sys.stdin.read()).get('status', 'FAIL'))
except Exception:
    print('FAIL')
")
      rest="$first_line"
      ;;
    *)
      status="FAIL"
      rest="$status_line"
      ;;
  esac

  STATUS[$name]="$status"
  DURATION[$name]="$elapsed"
  EVIDENCE[$name]="$rest"
  printf '%s\t%s\t%s\t%s\n' "$name" "$status" "$elapsed" "$rest" >>"$RESULTS_TSV"

  printf '[%s] %-22s %-9s (%ds)\n' "$CANARY_RUN_ID" "$name" "$status" "$elapsed"
  if [ "$status" = "FAIL" ]; then
    printf '    evidence: %s\n' "$rest"
  fi
}

usage() {
  cat <<'USAGE' >&2
Usage: canary.sh run [--rebuild-sha <sha>]
USAGE
  exit 2
}

cmd="${1:-}"
shift || true
case "$cmd" in
  run) ;;
  -h|--help) usage ;;
  *) usage ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --rebuild-sha) REBUILD_SHA="$2"; shift 2 ;;
    *) usage ;;
  esac
done

printf 'canary starting (run-id=%s, port=%s, data-dir=%s)\n' "$CANARY_RUN_ID" "$OMNIGENT_PORT" "$OMNIGENT_DATA_DIR"

# The 12 checks, in order.
run_check "01_boot"               "$CHECKS_DIR/01_boot.sh"
run_check "02_omniroute"          "$CHECKS_DIR/02_omniroute.py"
run_check "03_pi_repo_edit"       "$CHECKS_DIR/03_pi_repo_edit.sh"
run_check "04_pi_commit"          "$CHECKS_DIR/04_pi_commit.sh"
run_check "05_pi_push"            "$CHECKS_DIR/05_pi_push.sh"
run_check "06_opencode_repo_edit" "$CHECKS_DIR/06_opencode_repo_edit.sh"
run_check "07_opencode_commit"    "$CHECKS_DIR/07_opencode_commit.sh"
run_check "08_opencode_push"      "$CHECKS_DIR/08_opencode_push.sh"
run_check "09_langfuse"           "$CHECKS_DIR/09_langfuse.py"
run_check "10_verity_delegation"  "$CHECKS_DIR/10_verity_delegation.sh"
run_check "11_worktree_isolation" "$CHECKS_DIR/11_worktree_isolation.sh"
run_check "12_prod_url"           "$CHECKS_DIR/12_prod_url.py"

# ─── Report ────────────────────────────────────────────────────────
{
  printf '# Canary report — %s\n\n' "$CANARY_RUN_ID"
  printf '| Rebuild SHA | Run ID | Port | Data dir |\n'
  printf '| --- | --- | --- | --- |\n'
  printf '| `%s` | `%s` | `%s` | `%s` |\n\n' \
    "${REBUILD_SHA:-<unspecified>}" "$CANARY_RUN_ID" "$OMNIGENT_PORT" "$OMNIGENT_DATA_DIR"

  printf '## Summary\n\n'
  printf '| # | Check | Status | Duration |\n'
  printf '| --- | --- | --- | --- |\n'
  for i in 01 02 03 04 05 06 07 08 09 10 11 12; do
    case "$i" in
      01) name="01_boot" ;;
      02) name="02_omniroute" ;;
      03) name="03_pi_repo_edit" ;;
      04) name="04_pi_commit" ;;
      05) name="05_pi_push" ;;
      06) name="06_opencode_repo_edit" ;;
      07) name="07_opencode_commit" ;;
      08) name="08_opencode_push" ;;
      09) name="09_langfuse" ;;
      10) name="10_verity_delegation" ;;
      11) name="11_worktree_isolation" ;;
      12) name="12_prod_url" ;;
    esac
    status="${STATUS[$name]:-UNKNOWN}"
    duration="${DURATION[$name]:-0}"
    printf '| %s | `%s` | **%s** | %ss |\n' "$i" "$name" "$status" "$duration"
  done
  printf '\n'

  printf '## Evidence\n\n'
  for name in 01_boot 02_omniroute 03_pi_repo_edit 04_pi_commit 05_pi_push 06_opencode_repo_edit 07_opencode_commit 08_opencode_push 09_langfuse 10_verity_delegation 11_worktree_isolation 12_prod_url; do
    status="${STATUS[$name]:-UNKNOWN}"
    evidence="${EVIDENCE[$name]:-}"
    printf '### `%s` — %s\n\n' "$name" "$status"
    if [ -n "$evidence" ]; then
      printf '```\n%s\n```\n\n' "$evidence"
    fi
  done
} >"$REPORT_PATH"

# Cleanup.
rm -f "$RESULTS_TSV"

# Final verdict.
GREEN=1
for name in 01_boot 02_omniroute 03_pi_repo_edit 04_pi_commit 05_pi_push 06_opencode_repo_edit 07_opencode_commit 08_opencode_push 09_langfuse 10_verity_delegation 11_worktree_isolation 12_prod_url; do
  case "${STATUS[$name]:-UNKNOWN}" in
    PASS|SKIPPED) ;;
    *) GREEN=0 ;;
  esac
done

if [ "$GREEN" -eq 1 ]; then
  printf '\ncanary GREEN — report at %s\n' "$REPORT_PATH"
  exit 0
else
  printf '\ncanary RED — report at %s\n' "$REPORT_PATH"
  exit 1
fi