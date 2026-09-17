# Task success experiment

Phase 1 measures pre-task human expectations separately from task outcomes and
subjective feedback. This branch is stacked on `codex/rtx-peer-v2`; it does not
restore the historical O3 benchmark-floor UI from PRs #140/#156.

## Persistence

Existing `resource_event` conversation items carry version-1
`task-success-experiment` records. No tables, enum codes, indexes, migrations or
schema version change. Old application readers understand the existing envelope;
these non-content items are excluded from model history. The existing
conversation/type/position index supports paginated export.

The forecast uses an idempotent item ID derived from conversation and stable
input identity. It is committed before dispatch. Reusing that identity with a
different forecast/configuration is rejected; the original is never updated.
The record captures the selected configuration, actor, timestamp, input digest,
and input reference without copying prompt text. Default/unknown values remain
null. Native Codex reports its accepted RPC turn ID through the executor adapter;
a separate event links the attempt to the durable transcript response.

Outcomes append revisions with their raw four-way value. Success maps to 1,
Partial/Failed to 0, and Not sure to null. Subjective thumbs/comments retain their
existing separate storage and API. Caller-scoped experiment reads require READ;
outcome writes require EDIT and validate a durable completed assistant response.
Copied events in a fork retain source provenance and are excluded from the new
conversation's experiment projection.

Nullable fields reserve candidate set, policy version, selection propensity,
experiment source, and controlled exploration. Initial O3 fields remain null.
No calibration or model-quality advantage is asserted.

## Verification

From the task worktree on RTX:

```sh
.venv/bin/python -m pytest tests/stores/test_task_experiment.py tests/server/routes/test_task_experiment.py tests/stores/test_response_feedback.py tests/server/routes/test_response_feedback.py tests/inner/test_codex_native_executor.py tests/runtime/harnesses/test_executor_adapter.py tests/deploy/test_rtx_peer_v2.py -q
cd web
node node_modules/vitest/vitest.mjs run src/components/ResponseFeedbackActions.test.tsx src/shell/NewChatDialog.flow.test.tsx
```

Live acceptance must enter a blank or explicit 0–100 forecast, execute a bounded
Codex task, inspect the forecast/response link, then save and reload all four
outcomes, revise Not sure, and verify independent thumbs/comments. Repeat on both
peers after the supervised same-artifact rollout. Preserve prior histories,
cookies, state roots and rollback data.

Phase 2 shadow forecasting and optional experimental execution are not yet
implemented. Non-Codex response correlation and structured skill-invocation
forecast transport require additional coverage before broad harness acceptance.
