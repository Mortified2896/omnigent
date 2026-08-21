# Issue #133 rendered evidence

These screenshots come from the deterministic Playwright scenario in
`tests/e2e_ui/chat/test_terminal_reconciliation_visual.py` against the Vite dev build.
The test intercepts API/SSE boundaries; it does not use a production conversation.

1. [Running](01-running.png): the durable snapshot says the response is active.
2. [Reconnecting](02-reconnecting.png): the established stream closes before terminal/item
   application and the replacement connection is pending.
3. [Reconciled completed](03-reconciled-completed.png): the replacement stream opens,
   snapshot reconciliation reads the durable assistant item plus terminal response, renders the
   result once, and clears the running indicator.

Reproduction command (with a Vite dev server at `127.0.0.1:4173`):

```bash
OMNIGENT_ISSUE_133_UI_BASE_URL=http://127.0.0.1:4173 \
OMNIGENT_E2E_EVIDENCE_DIR=docs/evidence/issue-133 \
uv run pytest -q tests/e2e_ui/chat/test_terminal_reconciliation_visual.py \
  --browser chromium --browser-channel chrome
```
