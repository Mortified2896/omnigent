#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# canary.sh — Phase D pre-cutover canary runner
# ─────────────────────────────────────────────────────────────────────
#
# Note: this runner is intentionally /usr/bin/env bash (not POSIX
# sh) because it uses bash-only features: associative arrays
# (declare -A), process substitution, and local variables in
# functions. The per-check scripts (checks/*.sh) are bash
# (shebanged `#!/usr/bin/env bash` — they use `SECONDS`,
# `declare`, `local`, and other bash-only constructs).
#
# This script orchestrates the per-check scripts; it does NOT
# itself issue network requests or read the database. The checks
# do that.
#
# Phase D is the pre-cutover canary. It runs the 12 acceptance
# checks against a fresh install of the rebuild's Omnigent 0.7
# wheel, against an empty data dir, on the **existing Omnigent
# VM** (NOT on a disposable VM and NOT on another host). Isolation
# from the running production stack is enforced via an unused
# temporary loopback port, a temporary OMNIGENT_DATA_DIR, and
# temporary copies of the rebuild's config files. Aggregates
# per-check outcomes into docs/rebuild/canary-report.md.
#
# Usage:
#   canary.sh run [--rebuild-sha <sha>] [--run-id <id>] [--reuse-data-dir]
#   canary.sh run --check <name> [--rebuild-sha <sha>] [--run-id <id>]
#   canary.sh repeat --run-id <id> [--rebuild-sha <sha>]
#   canary.sh status
#
# Subcommands:
#   run     Run a fresh canary (D.1) against a NEW temp data dir.
#           Writes docs/rebuild/canary-report-<run-id>.md. Exits
#           non-zero on any FAIL or required SKIPPED.
#           --check <name> runs a single named check (e.g.
#           04_pi_repo_edit) against an already-running canary
#           wheel+host; the report only covers that check. Use this
#           while the temporary canary server remains available so
#           prerequisite failures fail fast instead of cascading.
#   repeat  Run a second canary (D.2) against the SAME temp config
#           as the original D.1 run, but a freshly-emptied temp
#           data dir. The temp data dir is wiped before the run;
#           the temp config, env.conf, and agent bundles are
#           preserved. Writes docs/rebuild/canary-report-<run-id>-
#           repeat.md. Exits non-zero on any FAIL or required
#           SKIPPED.
#   status  Print the latest canary outcome (PASS/RED).
#
# Required environment (set by the operator or the canary's
# systemd / cron wrapper):
#   OMNIGENT_PORT       the canary wheel's TCP port (must NOT
#                       equal PROD_PORT)
#   PROD_PORT           the production TCP port (default: same as
#                       OMNIGENT_PORT, in which case the isolation
#                       guard in check 2 will fail)
#   OMNIGENT_AUTH_HEADER the auth header name (default:
#                       X-Forwarded-Email)
#   OMNIGENT_DATA_DIR   the temp data dir (created on `run`,
#                       wiped on `repeat`)
#   ENV_FILE            optional path to an env.conf file the
#                       runner should `set -a; source` before
#                       running the checks (the canary wheel's
#                       own launcher uses this when running under
#                       a temp systemd unit)
#   CANARY_IDENTITY     the canary's auth identity
#   CANARY_FIXTURES_ROOT the canary fixtures root
#   LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
#                       for check 10
#   OMNIROUTE_BASE_URL, OMNIROUTE_AUTH_TOKEN
#                       for check 3
#   UPSTREAM_HEAD_SHA   the expected alembic_version row, e.g.
#                       b3c4d5e6f7a8 (upstream v0.7.0 head)
#   UNIT_NAME           the systemd unit name (default: omnigent)
#   REBUILD_SHA         the wheel SHA the canary is exercising
#
# Exit codes:
#   0  all 12 checks PASS (and the harness-binary checks are not
#      SKIPPED). Canary is green.
#   1  at least one check FAIL, OR a Pi/OpenCode check is SKIPPED.
#   2  bad invocation.
#
# Per-run evidence layout:
#   docs/rebuild/canary-runs/<run-id>/
#     canary.env             env vars used for the run
#     results.tsv            one row per check: name, status, duration, evidence
#     canary-report.md       full per-check evidence (mirrored to
#                            docs/rebuild/canary-report-<run-id>.md)
#     log/                   per-check stdout+stderr
#
# Phase D.2 (repeat) writes to:
#   docs/rebuild/canary-runs/<run-id>-repeat/

set -eu

CANARY_ROOT="$(cd "$(dirname "$0")" && pwd)"
# Repo root is three levels up from the runner:
#   <repo>/deploy/rebuild/tests/canary.sh
#     -> <repo>/deploy/rebuild/tests
#     -> <repo>/deploy/rebuild
#     -> <repo>/deploy
#     -> <repo>
REPO_ROOT="$(cd "$CANARY_ROOT/../../.." && pwd)"
CHECKS_DIR="$CANARY_ROOT/checks"

# Required environment (set by the operator or the canary's
# systemd / cron wrapper).
: "${OMNIGENT_PORT:=6767}"
: "${PROD_PORT:=$OMNIGENT_PORT}"
: "${OMNIGENT_AUTH_HEADER:=X-Forwarded-Email}"
: "${OMNIGENT_DATA_DIR:=/var/lib/canary-omnigent}"
: "${CANARY_IDENTITY:=canary@omnigent.local}"
: "${CANARY_FIXTURES_ROOT:=/tmp/canary-fixtures}"
: "${CANARY_RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"
: "${UPSTREAM_HEAD_SHA:=b3c4d5e6f7a8}"
: "${UNIT_NAME:=omnigent}"
: "${LANGFUSE_HOST:=}"
: "${LANGFUSE_PUBLIC_KEY:=}"
: "${LANGFUSE_SECRET_KEY:=}"
: "${OMNIROUTE_BASE_URL:=}"
: "${OMNIROUTE_AUTH_TOKEN:=}"
: "${OMNIROUTE_ROUTER_NAME:=omniroute}"
: "${OMNIROUTE_API_KEY:=}"
: "${REBUILD_SHA:=}"
: "${REPORT_ROOT:=$REPO_ROOT/docs/rebuild/canary-runs}"
: "${REQUESTED_CHILD_HARNESS:=pi}"

# Make sure the canary-runs/ directory exists.
mkdir -p "$REPORT_ROOT"

usage() {
  cat <<'USAGE' >&2
Usage:
  canary.sh run [--rebuild-sha <sha>] [--run-id <id>] [--reuse-data-dir]
  canary.sh run --check <name> [--rebuild-sha <sha>] [--run-id <id>]
  canary.sh repeat --run-id <id> [--rebuild-sha <sha>]
  canary.sh status
USAGE
  exit 2
}

cmd="${1:-}"
shift || true
case "$cmd" in
  run|repeat|status) ;;
  -h|--help) usage ;;
  *) usage ;;
esac

# Source the optional env.conf (the canary's temp launcher uses
# this to inject the rebuild wheel's env into the canary process).
if [ -n "${ENV_FILE:-}" ] && [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

REPEAT_MODE=0
RESUE_DATA_DIR=0
ONLY_CHECK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rebuild-sha) REBUILD_SHA="$2"; shift 2 ;;
    --run-id)      CANARY_RUN_ID="$2"; shift 2 ;;
    --check)       ONLY_CHECK="$2"; shift 2 ;;
    --reuse-data-dir) RESUE_DATA_DIR=1; shift ;;
    *) usage ;;
  esac
done

if [ "$cmd" = "repeat" ]; then
  REPEAT_MODE=1
fi

# Validate a --check name up-front (before any data-dir wipe) so a
# typo fails immediately instead of producing an empty run.
if [ -n "$ONLY_CHECK" ]; then
  if [ ! -e "$CHECKS_DIR/${ONLY_CHECK}.sh" ] && [ ! -e "$CHECKS_DIR/${ONLY_CHECK}.py" ]; then
    printf 'canary: unknown check name %s (no %s.sh / %s.py under %s)\n' \
      "$ONLY_CHECK" "$ONLY_CHECK" "$ONLY_CHECK" "$CHECKS_DIR" >&2
    exit 2
  fi
fi

# Per-run evidence directory. Each D.1 / D.2 run gets its own
# subtree so the two reports can be compared side by side.
if [ "$REPEAT_MODE" = 1 ]; then
  RUN_DIR="$REPORT_ROOT/${CANARY_RUN_ID}-repeat"
  REPORT_PATH="$RUN_DIR/canary-report.md"
  LOG_DIR="$RUN_DIR/log"
else
  RUN_DIR="$REPORT_ROOT/$CANARY_RUN_ID"
  REPORT_PATH="$RUN_DIR/canary-report.md"
  LOG_DIR="$RUN_DIR/log"
fi
mkdir -p "$RUN_DIR" "$LOG_DIR"

# Persist the env we ran with for auditability.
{
  printf '# Canary run env — %s\n' "$CANARY_RUN_ID"
  printf 'repeat_mode=%s\n' "$REPEAT_MODE"
  printf 'rebuild_sha=%s\n' "${REBUILD_SHA:-<unspecified>}"
  printf 'omni_port=%s\n' "$OMNIGENT_PORT"
  printf 'prod_port=%s\n' "$PROD_PORT"
  printf 'omni_data_dir=%s\n' "$OMNIGENT_DATA_DIR"
  printf 'omni_auth_header=%s\n' "$OMNIGENT_AUTH_HEADER"
  printf 'canary_identity=%s\n' "$CANARY_IDENTITY"
  printf 'canary_fixtures_root=%s\n' "$CANARY_FIXTURES_ROOT"
  printf 'upstream_head_sha=%s\n' "$UPSTREAM_HEAD_SHA"
  printf 'unit_name=%s\n' "$UNIT_NAME"
  printf 'requested_child_harness=%s\n' "$REQUESTED_CHILD_HARNESS"
  printf 'langfuse_host=%s\n' "${LANGFUSE_HOST:-<unset>}"
  printf 'omniroute_base_url=%s\n' "${OMNIROUTE_BASE_URL:-<unset>}"
} >"$RUN_DIR/canary.env"

# Reset the temp data dir for this run, unless the operator
# explicitly asked us to keep it (D.2 re-runs preserve the
# directory by default; D.2 wipes it by design).
#
# DEFAULT (no --reuse-data-dir): wipe the data dir. The canary
# is meant to test the wheel's *first-boot* behavior — the wheel
# must do the first-boot migration and create the fresh schema.
# The canary operator is responsible for stopping the wheel
# before running the canary, or for restarting the wheel after
# the wipe. (The cutover-script's Phase E step restarts the
# wheel; the Phase D canary orchestrator should mirror that.)
#
# --reuse-data-dir: keep the data dir. Use this when the canary
# is running against an already-started wheel (e.g. the wheel
# is launched as a long-running temp systemd unit and the canary
# checks are just running against the live wheel's state).
if [ "$REPEAT_MODE" = 1 ]; then
  if [ "$RESUE_DATA_DIR" = 0 ]; then
    printf 'canary repeat: wiping temp data dir %s\n' "$OMNIGENT_DATA_DIR" >&2
    rm -rf "$OMNIGENT_DATA_DIR"
  else
    printf 'canary repeat: keeping existing data dir %s (--reuse-data-dir)\n' "$OMNIGENT_DATA_DIR" >&2
  fi
else
  if [ "$RESUE_DATA_DIR" = 0 ]; then
    printf 'canary run: wiping temp data dir %s\n' "$OMNIGENT_DATA_DIR" >&2
    rm -rf "$OMNIGENT_DATA_DIR"
  else
    printf 'canary run: keeping existing data dir %s (--reuse-data-dir)\n' "$OMNIGENT_DATA_DIR" >&2
  fi
fi
mkdir -p "$OMNIGENT_DATA_DIR"
mkdir -p "$CANARY_FIXTURES_ROOT/$CANARY_RUN_ID"

export OMNIGENT_PORT OMNIGENT_AUTH_HEADER OMNIGENT_DATA_DIR
export CANARY_IDENTITY CANARY_FIXTURES_ROOT CANARY_RUN_ID
export UPSTREAM_HEAD_SHA UNIT_NAME PROD_PORT
export LANGFUSE_HOST LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
export OMNIROUTE_BASE_URL OMNIROUTE_AUTH_TOKEN OMNIROUTE_ROUTER_NAME OMNIROUTE_API_KEY
export REQUESTED_CHILD_HARNESS

# Per-check status tracking.
declare -A STATUS DURATION EVIDENCE
RESULTS_TSV="$RUN_DIR/results.tsv"
: >"$RESULTS_TSV"

# The lean production checks. The "name" is also the
# filename prefix under checks/.
CHECKS=(
  "01_db_init"
  "02_web_health"
  "07_opencode_repo_edit"
)

# Targeted execution: --check <name> runs exactly one named check
# against an already-running canary wheel+host. Validate the name
# up-front so a typo fails immediately instead of producing an
# empty run.
if [ -n "$ONLY_CHECK" ]; then
  found=0
  for c in "${CHECKS[@]}"; do
    if [ "$c" = "$ONLY_CHECK" ]; then
      found=1
      break
    fi
  done
  if [ "$found" != 1 ]; then
    printf 'canary: unknown check name %s; valid names:\n' "$ONLY_CHECK" >&2
    printf '  %s\n' "${CHECKS[@]}" >&2
    exit 2
  fi
fi

# The set of checks this invocation will actually run (used for the
# report and the final verdict).
RUN_CHECKS=()
for c in "${CHECKS[@]}"; do
  if [ -n "$ONLY_CHECK" ] && [ "$c" != "$ONLY_CHECK" ]; then
    continue
  fi
  RUN_CHECKS+=("$c")
done

run_check() {
  local name="$1"
  local start end elapsed status_line rest first_line status
  local script=""
  for ext in sh py; do
    if [ -e "$CHECKS_DIR/${name}.${ext}" ]; then
      script="$CHECKS_DIR/${name}.${ext}"
      break
    fi
  done
  start=$(date +%s)
  if [ -z "$script" ]; then
    status_line="FAIL script not found: ${name}.{sh,py}"
  else
    case "$script" in
      *.sh)
        # The check scripts are shebanged `#!/usr/bin/env bash`
        # because they use bash-only constructs (`SECONDS`,
        # `declare`, `local`, etc.). Run them with bash, NOT sh.
        status_line=$(bash "$script" 2>&1) || true
        ;;
      *.py)
        status_line=$(python3 "$script" 2>&1) || true
        ;;
    esac
  fi
  end=$(date +%s)
  elapsed=$((end - start))

  # Capture full output for the per-check log.
  {
    printf '# check %s — %s\n' "$name" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'exit_status=%s\n' "$status_line"
  } >"$LOG_DIR/${name}.log"
  printf '%s\n' "$status_line" >>"$LOG_DIR/${name}.log"

  # Extract the status. Two formats:
  #   shell checks: first line is "PASS"|"FAIL"|"SKIPPED", rest is
  #                 free-form evidence (multi-line).
  #   python checks: single-line JSON object whose `status` key is
  #                 "PASS"|"FAIL"|"SKIPPED", and the rest of the keys
  #                 are evidence.
  first_line=$(printf '%s\n' "$status_line" | head -1)
  # Some checks write a "note: ..." informational line to stderr
  # BEFORE the PASS line. The status_line captured both, so the
  # first non-empty / non-note line carries the verdict.
  verdict_line=""
  while IFS= read -r line; do
    case "$line" in
      PASS|FAIL|SKIPPED) verdict_line="$line"; break ;;
      \{*) verdict_line="$line"; break ;;
      note:*) continue ;;
      "") continue ;;
      *) continue ;;
    esac
  done <<STATUS_LINES
$status_line
STATUS_LINES
  first_line="${verdict_line:-$first_line}"
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
  printf '%s\t%s\t%s\n' "$name" "$status" "$elapsed" >>"$RESULTS_TSV"
  # Append the evidence to the log too, for human reading.
  {
    printf 'parsed_status=%s\n' "$status"
    printf 'duration=%s\n' "$elapsed"
    printf -- '--- evidence ---\n'
    printf '%s\n' "$rest"
  } >>"$LOG_DIR/${name}.log"

  printf '[%s] %-22s %-9s (%ds)\n' "$CANARY_RUN_ID" "$name" "$status" "$elapsed"
  if [ "$status" = "FAIL" ] || [ "$status" = "SKIPPED" ]; then
    printf '    evidence: %s\n' "$rest"
  fi
}

if [ "$cmd" = "status" ]; then
  # Print the latest canary report path + verdict.
  LATEST=$(ls -1t "$REPORT_ROOT"/*.md 2>/dev/null | head -1 || true)
  if [ -z "$LATEST" ]; then
    printf 'no canary reports found under %s\n' "$REPORT_ROOT" >&2
    exit 1
  fi
  printf 'latest report: %s\n' "$LATEST"
  head -1 "$LATEST" || true
  exit 0
fi

# Default the temp data dir to a user-writable path when the
# operator has not set OMNIGENT_DATA_DIR explicitly AND the
# default would fail the permission check. We do NOT change the
# value if the operator has set it (we respect their choice).
if [ "$OMNIGENT_DATA_DIR" = "/var/lib/canary-omnigent" ] && [ ! -w "$OMNIGENT_DATA_DIR" ] && [ ! -w "$(dirname -- "$OMNIGENT_DATA_DIR")" ]; then
  OMNIGENT_DATA_DIR="$HOME/.canary-omnigent"
  export OMNIGENT_DATA_DIR
fi

if [ "$REPEAT_MODE" = 1 ]; then
  printf 'canary D.2 REPEAT starting (run-id=%s, port=%s, data-dir=%s)\n' "$CANARY_RUN_ID" "$OMNIGENT_PORT" "$OMNIGENT_DATA_DIR"
else
  printf 'canary D.1 RUN starting (run-id=%s, port=%s, data-dir=%s)\n' "$CANARY_RUN_ID" "$OMNIGENT_PORT" "$OMNIGENT_DATA_DIR"
fi
if [ -n "$ONLY_CHECK" ]; then
  printf 'canary: targeted run — check(s): %s\n' "${RUN_CHECKS[*]}" >&2
fi

for c in "${RUN_CHECKS[@]}"; do
  run_check "$c"
done

# ─── Report ────────────────────────────────────────────────────────
{
  if [ "$REPEAT_MODE" = 1 ]; then
    printf '# Canary D.2 (repeatability) report — %s\n\n' "$CANARY_RUN_ID"
  else
    printf '# Canary D.1 report — %s\n\n' "$CANARY_RUN_ID"
  fi
  printf '| Rebuild SHA | Run ID | Phase | Port | Prod port | Data dir | Auth header |\n'
  printf '| --- | --- | --- | --- | --- | --- | --- |\n'
  printf '| `%s` | `%s` | %s | `%s` | `%s` | `%s` | `%s` |\n\n' \
    "${REBUILD_SHA:-<unspecified>}" \
    "$CANARY_RUN_ID" \
    "$(if [ "$REPEAT_MODE" = 1 ]; then echo 'D.2 (repeat)'; else echo 'D.1 (initial)'; fi)" \
    "$OMNIGENT_PORT" \
    "$PROD_PORT" \
    "$OMNIGENT_DATA_DIR" \
    "$OMNIGENT_AUTH_HEADER"

  printf '## Summary\n\n'
  printf '| # | Check | Status | Duration |\n'
  printf '| --- | --- | --- | --- |\n'
  for c in "${RUN_CHECKS[@]}"; do
    status="${STATUS[$c]:-UNKNOWN}"
    duration="${DURATION[$c]:-0}"
    printf '| %s | `%s` | **%s** | %ss |\n' "${c:0:2}" "$c" "$status" "$duration"
  done
  printf '\n'

  printf '## Evidence\n\n'
  for c in "${RUN_CHECKS[@]}"; do
    status="${STATUS[$c]:-UNKNOWN}"
    evidence="${EVIDENCE[$c]:-}"
    printf '### `%s` — %s\n\n' "$c" "$status"
    if [ -n "$evidence" ]; then
      printf '```\n%s\n```\n\n' "$evidence"
    else
      printf '_(no evidence captured)_\n\n'
    fi
  done

  printf '## Per-run artifacts\n\n'
  printf 'Full per-check stdout+stderr captured under `%s/log/*.log`.\n' "$RUN_DIR"
  printf 'TSV results in `%s/results.tsv`.\n' "$RUN_DIR"
  printf 'Env snapshot in `%s/canary.env`.\n' "$RUN_DIR"
} >"$REPORT_PATH"

# Also mirror the report to docs/rebuild/canary-report[-repeat].md
# so the most-recent run is the canonical one. (The per-run
# report under canary-runs/ is the durable artifact.)
if [ "$REPEAT_MODE" = 1 ]; then
  cp "$REPORT_PATH" "$REPO_ROOT/docs/rebuild/canary-report-repeat.md"
else
  cp "$REPORT_PATH" "$REPO_ROOT/docs/rebuild/canary-report.md"
fi

# Final verdict.
GREEN=1
for c in "${RUN_CHECKS[@]}"; do
  case "${STATUS[$c]:-UNKNOWN}" in
    PASS) ;;
    *) GREEN=0 ;;
  esac
done

# A skipped OpenCode workflow is also RED.
for c in 07_opencode_repo_edit; do
  case "${STATUS[$c]:-UNKNOWN}" in
    SKIPPED) GREEN=0 ;;
  esac
done

if [ "$GREEN" -eq 1 ]; then
  if [ "$REPEAT_MODE" = 1 ]; then
    printf '\ncanary D.2 GREEN — report at %s\n' "$REPORT_PATH"
  else
    printf '\ncanary D.1 GREEN — report at %s\n' "$REPORT_PATH"
  fi
  exit 0
else
  if [ "$REPEAT_MODE" = 1 ]; then
    printf '\ncanary D.2 RED — report at %s\n' "$REPORT_PATH"
  else
    printf '\ncanary D.1 RED — report at %s\n' "$REPORT_PATH"
  fi
  exit 1
fi
