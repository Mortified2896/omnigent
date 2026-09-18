# Task outcomes

Completed durable responses expose four human outcome labels:

- `Success`
- `Partial`
- `Failed`
- `Not sure`

`Success` records first-attempt success as 1. `Partial` and `Failed` record
0. `Not sure` leaves the value unset. Revisions remain append-only and may be
changed later.

The current UI intentionally does not expose percentage estimators, thumbs, or
the retired shadow forecast surface. Existing historical data remains readable;
this feature does not require deleting old records.

## Verification

Run the focused backend and frontend tests for outcome persistence and editing:

```sh
.venv/bin/python -m pytest \
  tests/stores/test_task_experiment.py \
  tests/server/routes/test_task_experiment.py \
  tests/stores/test_response_feedback.py \
  tests/server/routes/test_response_feedback.py -q

cd web
node node_modules/vitest/vitest.mjs run \
  src/components/ResponseFeedbackActions.test.tsx \
  src/shell/NewChatDialog.flow.test.tsx
```

This document is a feature contract, not a deployment record. Verify live UI
behavior against the actual running Omnigent instance when a task requires live
acceptance.
