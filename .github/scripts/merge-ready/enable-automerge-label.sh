#!/usr/bin/env bash
# Enables GitHub auto-merge only for a still-open, exact-head labeled PR.
# Removing `automerge` prevents a new attempt but does not disable an attempt
# already queued in GitHub; this is intentionally one-way intent.
#
# Env in: GH_TOKEN, REPO, PR, SHA

set -euo pipefail

set +e
PR_JSON=$(gh pr view "$PR" --repo "$REPO" \
  --json state,headRefOid,labels 2>/dev/null)
VIEW_RC=$?
set -e
if [[ $VIEW_RC -ne 0 ]]; then
  echo "::warning::Could not revalidate PR #$PR; auto-merge was not enabled."
  exit 0
fi

STATE=$(jq -r '.state' <<<"$PR_JSON")
HEAD=$(jq -r '.headRefOid' <<<"$PR_JSON")
if [[ "$STATE" != "OPEN" || "$HEAD" != "$SHA" ]]; then
  echo "::warning::PR #$PR is no longer open at $SHA; auto-merge was not enabled."
  exit 0
fi
if ! jq -e 'any(.labels[]?; .name == "automerge")' <<<"$PR_JSON" >/dev/null; then
  echo "::notice::PR #$PR no longer has 'automerge'; no new enable attempt was made."
  exit 0
fi

set +e
MERGE_OUT=$(gh pr merge "$PR" --repo "$REPO" --squash --auto \
  --match-head-commit "$SHA" 2>&1)
MERGE_RC=$?
set -e
echo "$MERGE_OUT"

if [[ $MERGE_RC -eq 0 ]]; then
  echo "::notice::Auto-merge enabled via 'automerge' label."
else
  echo "::warning::Could not enable auto-merge for exact head $SHA: $MERGE_OUT"
fi
