# Code review for the custom fork

Follow [AGENTS.md](../AGENTS.md) for `Mortified2896/omnigent`. These instructions
apply when a review is requested; they do not require starting an automated
reviewer or a second agent for each task. Automated task scoring remains subject
to the separate safety rules in AGENTS.md.

Review correctness, regressions, security, and whether verification matches the
changed behavior. Cite actionable findings with file/line evidence.

- Prefer focused tests in the affected area. Existing tests can suffice when
  they already cover the change; request a regression test for a bug or an
  uncovered behavior change.
- Backend tests normally belong in the matching `tests/` area; frontend tests
  normally use colocated Vitest tests. Use integration or Playwright/E2E tests
  when cross-component or browser behavior needs them, not merely because a
  feature or UI file changed.
- Docs, styling, or copy changes with no behavioral effect do not automatically
  need a new test. Do not request trivial tests just to satisfy a checklist.
- Do not block an ordinary fork review solely for a missing issue, upstream PR
  template, DCO trailer, diagram, demo recording, screenshot, or full-suite run.
  Distinguish an actual coverage/safety gap from an upstream process preference.
- Check authorization, preservation of unrelated work, and deployment/data
  safeguards when relevant. A small diff can still need substantial verification.

Existing CI and repository protection remain enforceable. Report conflicting
required gates explicitly; these instructions do not disable or waive them.
For an explicitly requested upstream contribution, use upstream's current
contribution and review requirements instead of assuming this fork's defaults.
