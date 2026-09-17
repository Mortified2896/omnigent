# Task outcomes

Completed durable responses offer Success, Partial, Failed and Not sure.
Success means the task was accomplished without material correction or retry.
Partial/Failed derive first-attempt success 0; Success derives 1; Not sure stays
null and can be revised later. Every revision appends an existing resource-event
conversation item. No database migration is introduced.

The owner requested removal of percentage estimators and thumbs after the first
experiment rollout. The composer no longer collects probabilities, the response
toolbar has no thumbs or forecast audit, and the O3 shadow forecaster is removed.
Both peers have the shadow flag disabled. Existing forecasts, ratings, comments,
conversation histories and rollback artifacts remain preserved. Legacy storage
and read APIs remain compatible; this change does not delete recorded data.

## Verify

Open either peer, create a new task, and confirm the model controls have no
probability input. Open a completed saved response: only the four outcome choices
are shown beside the normal copy/branch controls. Select Not sure, reload, then
select Success and reload again.

Focused checks on RTX:

```sh
.venv/bin/python -m pytest tests/stores/test_task_experiment.py tests/server/routes/test_task_experiment.py tests/stores/test_response_feedback.py tests/server/routes/test_response_feedback.py -q
cd web
node node_modules/vitest/vitest.mjs run src/components/ResponseFeedbackActions.test.tsx src/shell/NewChatDialog.flow.test.tsx
```

HomeLab's `accept-outcomes-browser.py` verifies the saved response on desktop and
mobile, all four outcome revisions across reload, and absence of percentages,
O3 audit and thumbs without making model calls.
