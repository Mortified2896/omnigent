#!/usr/bin/env bash
# Single source of truth for the Merge Ready outcome. It emits `state`,
# `short_desc`, and compatibility `long_desc` outputs.
#
# The gate is green iff every required check is green. There is no CI bypass;
# `Maintainer Approval` remains a separate branch-protection check.
#
#   CI eval  | state    | meaning
#   ---------+----------+----------------------------
#   success  | success  | all required checks green
#   failure  | failure  | a required check is red
#
# Env in: EVAL, FAILED
# Out:    state, short_desc, long_desc on $GITHUB_OUTPUT

set -euo pipefail

if [[ "$EVAL" == "success" ]]; then
  STATE=success
  SHORT="All required checks green"
  LONG=":white_check_mark: gate is green. A current open PR with \`automerge\` intent may now queue exact-head auto-merge."
else
  STATE=failure
  SHORT="Required checks not all green"
  LONG=$':hourglass: gate not green yet. Required checks not satisfied:\n\n'"$FAILED"$'\nExact-head auto-merge remains disabled until a later lifecycle evaluation is green.'
fi

# GitHub commit-status descriptions max out at 140 chars.
if [[ ${#SHORT} -gt 140 ]]; then
  SHORT="${SHORT:0:137}..."
fi

{
  echo "state=$STATE"
  echo "short_desc=$SHORT"
  echo "long_desc<<_LONG_EOF_"
  printf '%s' "$LONG"
  echo
  echo "_LONG_EOF_"
} >> "$GITHUB_OUTPUT"
echo "Computed gate: state=$STATE | $SHORT"
