#!/usr/bin/env bash
# Evaluates REQUIRED check runs on the PR head SHA. Only pull-request suites
# from each check's producer count, and any nonterminal producer run blocks.
#
# The newest producer suite's row wins. Empty successful/skipped no-op suites
# may fall back, but an intervening failed suite without that row stays red.
#
# Env in: GH_TOKEN, REPO, SHA
# Out:    failed=<markdown bullet list of failed names> on $GITHUB_OUTPUT
# Exit:   0 if all green, 1 if any red.

set -euo pipefail

HERE=$(dirname "$0")
# shellcheck disable=SC1091
source "$HERE/required.sh"

CHECKS=$(gh api "repos/$REPO/commits/$SHA/check-runs?filter=all&per_page=100" --paginate \
  --jq '.check_runs[] | [.name, .status, (.conclusion // "null"), (.completed_at // .started_at // ""), ((.check_suite.id // "") | tostring), (.id | tostring)] | @tsv')

# Per-workflow run state for this SHA (one row per run: name, status,
# conclusion, created_at, check_suite_id, id, run_attempt). Only pull_request
# runs may govern the PR gate; a push/manual run must not vouch for or block it.
WORKFLOW_RUNS=$(gh api "repos/$REPO/actions/runs?head_sha=$SHA&per_page=100" --paginate \
  --jq '.workflow_runs[] | select(.event == "pull_request") | [.name, .status, (.conclusion // "null"), (.created_at // ""), ((.check_suite_id // "") | tostring), (.id | tostring), ((.run_attempt // "") | tostring)] | @tsv')

# GitHub reruns reuse a workflow run's check_suite_id. Restrict accepted rows
# to check-run IDs exposed by the exact current attempt, never a prior attempt.
declare -A ATTEMPT_CHECK_IDS=()
declare -A ATTEMPT_LOAD_STATE=()

load_attempt_check_ids() {
  local row="$1" run_id run_attempt key ids check_id
  run_id=$(printf '%s' "$row" | cut -f6)
  run_attempt=$(printf '%s' "$row" | cut -f7)
  if ! [[ "$run_id" =~ ^[1-9][0-9]*$ && "$run_attempt" =~ ^[1-9][0-9]*$ ]]; then
    echo "Malformed workflow run ID/attempt: $run_id/$run_attempt" >&2
    return 1
  fi
  key="$run_id:$run_attempt"
  if [[ "${ATTEMPT_LOAD_STATE[$key]+loaded}" == "loaded" ]]; then
    [[ "${ATTEMPT_LOAD_STATE[$key]}" == "ok" ]]
    return
  fi
  if ! ids=$(gh api \
    "repos/$REPO/actions/runs/$run_id/attempts/$run_attempt/jobs?per_page=100" \
    --paginate \
    --jq '.jobs[] | if (.check_run_url // "") == "" then "__INVALID__" else (.check_run_url | split("/")[-1]) end'); then
    ATTEMPT_LOAD_STATE["$key"]="error"
    echo "Could not load exact-attempt jobs for run $run_id attempt $run_attempt" >&2
    return 1
  fi
  while IFS= read -r check_id; do
    [[ -z "$check_id" ]] && continue
    if ! [[ "$check_id" =~ ^[1-9][0-9]*$ ]]; then
      ATTEMPT_LOAD_STATE["$key"]="error"
      echo "Malformed check-run ID for run $run_id attempt $run_attempt: $check_id" >&2
      return 1
    fi
  done <<<"$ids"
  ATTEMPT_CHECK_IDS["$key"]="$ids"
  ATTEMPT_LOAD_STATE["$key"]="ok"
}

SELECTED_ROW=""
select_check_for_run() {
  local name="$1" producer_row="$2" suite run_id run_attempt key ids check_row check_id
  SELECTED_ROW=""
  suite=$(printf '%s' "$producer_row" | cut -f5)
  run_id=$(printf '%s' "$producer_row" | cut -f6)
  run_attempt=$(printf '%s' "$producer_row" | cut -f7)
  if ! [[ "$suite" =~ ^[1-9][0-9]*$ ]]; then
    return 1
  fi
  if ! load_attempt_check_ids "$producer_row"; then
    return 1
  fi
  key="$run_id:$run_attempt"
  ids="${ATTEMPT_CHECK_IDS[$key]}"
  while IFS= read -r check_row; do
    [[ -z "$check_row" ]] && continue
    check_id=$(printf '%s' "$check_row" | cut -f6)
    if grep -qxF -- "$check_id" <<<"$ids"; then
      SELECTED_ROW="$check_row"
      return 0
    fi
  done < <(printf '%s\n' "$CHECKS" \
    | awk -F'\t' -v n="$name" -v s="$suite" '$1 == n && $5 == s {print}' \
    | sort -t $'\t' -k4,4r -k6,6nr)
}

# Classifies missing ALLOW_SKIP rows: no run, completed success, and completed
# skip may justify absence; every other terminal outcome remains red.
newest_workflow_run() {
  local wf="$1"
  printf '%s\n' "$WORKFLOW_RUNS" | awk -F'\t' -v w="$wf" '$1 == w' \
    | sort -t $'\t' -k4,4 -k6,6n | tail -n 1
}

newest_nonterminal_workflow_run() {
  local wf="$1"
  printf '%s\n' "$WORKFLOW_RUNS" \
    | awk -F'\t' -v w="$wf" '$1 == w && $2 != "completed"' \
    | sort -t $'\t' -k4,4 -k6,6n | tail -n 1
}

workflow_run_outcome() {
  local row="$1" status concl
  if [[ -z "$row" ]]; then
    echo "none"
    return
  fi
  status=$(printf '%s' "$row" | cut -f2)
  concl=$(printf '%s' "$row" | cut -f3)
  if [[ "$status" == "completed" && "$concl" == "success" ]]; then
    echo "success"
  elif [[ "$status" == "completed" && "$concl" == "skipped" ]]; then
    echo "skipped"
  else
    echo "other"
  fi
}

FAIL=0
FAILED_LINES=""
for n in "${REQUIRED[@]}"; do
  wf=$(workflow_for "$n")
  WF_ROW=$(newest_workflow_run "$wf")
  outcome=$(workflow_run_outcome "$WF_ROW")
  ACTIVE_ROW=$(newest_nonterminal_workflow_run "$wf")
  if [[ -n "$ACTIVE_ROW" ]]; then
    WF_STATUS=$(printf '%s' "$ACTIVE_ROW" | cut -f2)
    WF_CONCL=$(printf '%s' "$ACTIVE_ROW" | cut -f3)
    echo "NOT GREEN: $n  (pull_request workflow '$wf' is status=$WF_STATUS, conclusion=$WF_CONCL)"
    FAILED_LINES+="- \`$n\` (producer workflow is status=$WF_STATUS, conclusion=$WF_CONCL)"$'\n'
    FAIL=1
    continue
  fi

  # A row is current only when the exact latest attempt exposes its check ID.
  # This rejects old attempts and same-SHA push/workflow_dispatch suites.
  ROW=""
  if [[ -n "$WF_ROW" ]]; then
    if ! select_check_for_run "$n" "$WF_ROW"; then
      echo "NOT GREEN: $n  (could not inspect the producer's exact workflow attempt)"
      FAILED_LINES+="- \`$n\` (could not inspect the producer's exact workflow attempt)"$'\n'
      FAIL=1
      continue
    fi
    ROW="$SELECTED_ROW"
  fi
  FALLBACK_BLOCKED=false
  FALLBACK_LOOKUP_FAILED=false
  if [[ -z "$ROW" && ( "$outcome" == "success" || "$outcome" == "skipped" ) ]]; then
    # Walk producer runs, not check completion times. Empty successful no-op
    # suites may fall back; an empty failed run remains decisive.
    while IFS= read -r PRODUCER_ROW; do
      if ! select_check_for_run "$n" "$PRODUCER_ROW"; then
        FALLBACK_LOOKUP_FAILED=true
        break
      fi
      CANDIDATE="$SELECTED_ROW"
      if [[ -n "$CANDIDATE" ]]; then
        ROW="$CANDIDATE"
        break
      fi
      PRODUCER_OUTCOME=$(workflow_run_outcome "$PRODUCER_ROW")
      if [[ "$PRODUCER_OUTCOME" == "other" ]]; then
        FALLBACK_BLOCKED=true
        FALLBACK_STATUS=$(printf '%s' "$PRODUCER_ROW" | cut -f2)
        FALLBACK_CONCL=$(printf '%s' "$PRODUCER_ROW" | cut -f3)
        break
      fi
    done < <(printf '%s\n' "$WORKFLOW_RUNS" \
      | awk -F'\t' -v w="$wf" '$1 == w && $5 != ""' \
      | sort -t $'\t' -k4,4r -k6,6nr)
  fi
  if [[ "$FALLBACK_LOOKUP_FAILED" == "true" ]]; then
    echo "NOT GREEN: $n  (could not inspect a producer workflow attempt)"
    FAILED_LINES+="- \`$n\` (could not inspect a producer workflow attempt)"$'\n'
    FAIL=1
    continue
  fi
  if [[ "$FALLBACK_BLOCKED" == "true" ]]; then
    echo "NOT GREEN: $n  (newer workflow '$wf' is status=$FALLBACK_STATUS, conclusion=$FALLBACK_CONCL with no check row)"
    FAILED_LINES+="- \`$n\` (producer workflow is status=$FALLBACK_STATUS, conclusion=$FALLBACK_CONCL and emitted no matching check)"$'\n'
    FAIL=1
    continue
  fi
  if [[ -z "$ROW" ]]; then
    if is_allow_skip "$n"; then
      if [[ "$outcome" == "other" ]]; then
        echo "NOT GREEN: $n  (workflow '$wf' did not succeed and the current-suite check is missing)"
        FAILED_LINES+="- \`$n\` (producer workflow did not succeed and its current-suite check is missing)"$'\n'
        FAIL=1
        continue
      fi
      # outcome is "none" (workflow path-skipped), "success" (job
      # conditionally excluded from a green run), or "skipped" (whole
      # workflow gated off, e.g. fork/draft PR) — all legitimate.
      echo "OK      : $n  (skipped: path-ignored, conditionally-excluded, or fork/draft-gated)"
      continue
    fi
    echo "MISSING : $n"
    FAILED_LINES+="- \`$n\` (not yet started or not configured on this commit)"$'\n'
    FAIL=1
    continue
  fi
  STATUS=$(echo "$ROW" | cut -f2)
  CONCL=$(echo "$ROW" | cut -f3)
  if [[ "$STATUS" != "completed" ]]; then
    echo "NOT GREEN: $n  (status=$STATUS, conclusion=$CONCL)"
    FAILED_LINES+="- \`$n\` (still running, status=$STATUS)"$'\n'
    FAIL=1
  elif [[ "$CONCL" == "skipped" ]] && is_allow_skip "$n"; then
    echo "OK      : $n  (skipped via path filter)"
  elif [[ "$CONCL" != "success" ]]; then
    echo "NOT GREEN: $n  (status=$STATUS, conclusion=$CONCL)"
    FAILED_LINES+="- \`$n\` (conclusion=$CONCL)"$'\n'
    FAIL=1
  else
    echo "OK      : $n"
  fi
done

{
  echo "failed<<_FAILED_EOF_"
  printf '%s' "$FAILED_LINES"
  echo "_FAILED_EOF_"
} >> "$GITHUB_OUTPUT"

if [[ $FAIL -eq 1 ]]; then
  echo ""
  echo "Required checks are not all green on $SHA."
  exit 1
fi

echo "All required checks green on $SHA."
