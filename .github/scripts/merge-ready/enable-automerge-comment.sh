#!/usr/bin/env bash
# Records an authorized `/merge` as one-way `automerge` intent, then emits a
# repository dispatch that evaluates and acts on the exact current head.
# GITHUB_TOKEN label changes do not themselves start pull_request workflows.
#
# Env in: GH_TOKEN, REPO, PR, SHA, AUTHOR

set -euo pipefail

gh api --method POST "repos/$REPO/issues/$PR/labels" \
  -f 'labels[]=automerge' >/dev/null

# repository_dispatch is one of the events that GITHUB_TOKEN may trigger.
# Keep PR and SHA as strings; the receiver validates both against live state.
gh api --method POST "repos/$REPO/dispatches" \
  -f event_type=merge-ready-request \
  -f "client_payload[pr]=$PR" \
  -f "client_payload[sha]=$SHA" >/dev/null

BODY=":robot: \`/merge\` from @$AUTHOR recorded \`automerge\` intent for \`$SHA\`. Merge Ready is re-evaluating that exact head."
gh pr comment "$PR" --repo "$REPO" --body "$BODY"
