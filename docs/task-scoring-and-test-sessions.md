# Scoring eligibility, blind review input, and live-test sessions

## Independent state

The four human outcomes remain `success`, `partial`, `failed`, and `not_sure`.
Eligibility is a separate append-only `scoring_eligibility` resource event in the
existing task-experiment envelope. An unrated answer can be excluded without
inventing an outcome. Excluding or restoring an answer does not edit outcome
revisions, human tags/comments, subjective feedback, or transcript content.

The same completed-answer validation and caller permissions apply to both write
surfaces. An eligibility change cannot overwrite concurrent outcome/comment/tag
edits, because they are different event kinds. Read projections use append order,
not second-resolution timestamps, to select the last revision per caller/response.

- `GET /v1/sessions/{id}/scoring-policy`: session-wide defaults, test retention,
  and the caller's latest response exclusions.
- `PUT /v1/sessions/{id}/scoring-eligibility/{response_id}`: strict boolean
  `score_eligible`, optional `exclusion_reason` (`test_fixture`, `duplicate`,
  `out_of_scope`, `other`). An included response cannot carry an exclusion reason.
- `GET /v1/sessions/{id}/scored-outcomes`: numerical projection of the caller's
  latest included, known human outcomes. No tags or comments are returned.

Legacy ordinary sessions/responses without eligibility metadata default to
included. Not sure and unrated answers are missing observations, not failures.
A session-wide exclusion always wins over response inclusion. Excluding a previous
Success removes it from the projection without erasing the original Success.
Do not average or count the raw revision log. Consumers must rebuild/invalidate
cached projections after exclusion changes and fetch current session labels.

No database migration is required. Older clients do not display eligibility;
older numerical consumers that ignore it are NOT safe scoring consumers.

## Human tags stay visible; the scoring AI stays blind

Keep the existing review tags/comments editable in the UI, including on excluded
answers. They are human-review metadata, not extra task instructions.

`build_blind_scoring_input` is an allowlist boundary. Its v1 payload contains only
`schema_version`, the original `task`, and the completed `answer`. It drops every
other field, including human outcomes, tags, comments, exclusion reasons, labels,
session titles, test IDs, retention, model identity, and nested metadata/history.
Tests assert identical model payloads when only those fields change.

This builder is not an enabled AI evaluator or a claim of end-to-end blindness.
Before adding an evaluator, gate eligibility OUTSIDE the prompt and wire the
builder at the actual outbound request boundary. Do not fork a context containing
human reviews or let evaluator tools retrieve raw sessions/review events. Keep the
existing automated-review safety gate disabled. Any future machine-verification
evidence needs a reviewed, typed allowlist extension, not generic context passthrough.
Text explicitly included by the user in the original task is still task text;
this boundary isolates stored review metadata, not arbitrary semantic content.

## Test-session lifecycle

Prefer isolated `omnidev` or test-fixture databases. For an explicitly authorized
live acceptance test, use a unique run ID and persist a private creation manifest
bound to the exact server/instance before teardown. Record each returned session
ID; never find deletable sessions by title, age, model, or text.

Merge `test_session_labels(run_id, creator)` into session-create labels before the
first test prompt. It writes:

```text
omnigent.test.run_id       = <unique run ID>
omnigent.test.created_by   = codex (or the actual harness)
omnigent.test.retention    = ephemeral
omnigent.scoring.eligible  = false
```

Any test-run marker excludes the whole session, even if a stale response record
says included. Labels are hygiene metadata, not proof of authorization.

After testing, call `plan_test_cleanup` with the creation manifest, explicit
verification result, and fresh session metadata. It is deliberately a pure planner:

- `preserve`: unknown/foreign/unmarked/changed/pinned/active session, uncertain
  state, or a parent/child tree not proven isolated. Do not mutate it.
- `keep_for_inspection`: failure, unexpected/unverified result, or explicit hold.
  Merge `keep_for_inspection_labels(reason)` and retain the manifest/evidence.
  The response UI displays “Test evidence · Kept for inspection”. The live script
  should also use a visible sidebar marker or a clearly prefixed title so a failed
  session without a completed answer is discoverable.
- `delete_candidate`: a verified, unchanged, ephemeral, isolated session created
  by this run. This is NOT permission to call an unconditional bulk delete.

Before deletion, revalidate ownership, exact instance, activity, edits, pins, and
all descendants at the mutation boundary. Prove an atomic/conditional deletion
contract or preserve the session; a stale client read is insufficient. A reversible
archive may be offered after fresh checks, but archiving can stop a runner and is
not a harmless substitute for ownership/activity checks. Never request Git branch,
worktree, artifact, or unrelated database cleanup as part of chat cleanup.

The planner does not execute cleanup. Existing unmarked chats are not automatically
backfilled or deleted. Retention holds and uncertain sessions remain available.

## Remaining integration before adoption

1. Fetch GitHub, inspect branch/base/dirty work, and fast-forward the correct task
   branch before editing. Reconcile new main changes without resetting local work.
2. Run the full-checkout backend, frontend, and browser tests below; regenerate
   `openapi.json` with the repository's supported generator and inspect only the
   intentional contract changes. Capture desktop/mobile screenshots.
3. Wire test creation and teardown in the actual acceptance scripts. Operational
   HomeLab scripts belong in `Mortified2896/HomeLab`, with its own AGENTS/worktree.
   Inspect current scripts before changing them. Add a manifest, visible retained
   evidence marker, idempotent teardown, and mutation-boundary race tests. Do not
   execute a broad live cleanup.
4. Audit every existing/future scoring consumer for the numerical projection and
   outbound allowlist. Add an actual outbound-payload capture test before enabling
   any scorer; keep unrelated routing and evaluator safety work separate.
5. Keep the PR draft until these checks pass. No merge, deployment, service restart,
   provider inference, or production-data mutation is implied by this source work.

Suggested focused checks (normal repository development environment):

```sh
uv run --no-sync pytest tests/server/test_task_scoring.py \
  tests/util/test_test_session_policy.py \
  tests/server/routes/test_scoring_eligibility.py \
  tests/server/routes/test_task_experiment.py \
  tests/stores/test_task_experiment.py -q
pnpm --dir web exec vitest run src/components/ResponseScoringActions.test.tsx \
  src/components/ResponseFeedbackActions.test.tsx
uv run --no-sync pytest tests/e2e_ui/chat/test_scoring_eligibility.py \
  tests/e2e_ui/chat/test_task_outcome_edits.py -q
pnpm --dir web run lint
pnpm --dir web run type-check
pnpm --dir web run build
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyrefly check
```
