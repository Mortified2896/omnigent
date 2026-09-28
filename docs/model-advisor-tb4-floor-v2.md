# TB4 floor v2: isolated source preparation and ZCode continuation

Status: **draft preparation, not wired into the composer/API/host and not deployed**.
Source base inspected: `6637940468bbb28c167746b85821a2fc66146abb` on
`Mortified2896/omnigent/main`. This is evidence, not permission to reuse stale refs.
Task branch: `codex/tb4-floor-paid-free-20260922`.

## Approved product change

Replace the concrete *proposal* with an independent numeric Terminal-Bench 4
minimum-score proposal. Keep concrete model/effort/lane identity for execution.
The execution pool has exactly **Paid only | Free only**. Paid only retains the
existing user-selected ChatGPT-plan/OpenAI and GLM-plan model/effort allowlist.
Free only uses authenticated, qualified free connections, not model-name guesses.
A subscription is paid access even when a request has no extra cash charge.
Unknown billing never qualifies as free. Toggling modes must not erase paid picks.

The advisor remains independently selectable. The toggle is explicitly labeled
**Execution pool**; a selected paid advisor in Free only must have a prominent
allowance notice. Do not switch advisors or consume paid allowance invisibly.
For an entirely free round, both advisor and executor need qualified free lanes.
Unsupported tool-free advisor transports must block, not fall back to paid.

The floor advisor sees the task and a fixed TB4 definition, **not** the available
models, their scores, ranking/cost/quota, paid/free mode, human floor, assignment,
or stored human feedback. Never send a private frozen snapshot to inference.
No invented calibration anchors or model-derived difficulty categories. Literal
floors are operational hypotheses, not calibrated task difficulty or task-success
probabilities. This does not prove the advisor has no model knowledge in training.

A floor admits candidates with reported overall pass@1 >= the literal threshold.
Raising the floor **removes** lower-scoring candidates; stronger candidates were
already eligible at lower floors. Selection can change when a formerly preferred
candidate is removed. Preserve the existing deterministic router priority within
the admitted set. Do not introduce highest-TB4-wins, TB4/price ratios, a new paid
meta-router, or fabricated subscription/token costs.

Human/advisor floors are frozen independently; default 50/50 on disagreement.
Retain the existing balance control. Equal floors are agreement with probability 1.
Different floors resolving to the same executor are *not* proposal agreement.
One initial task/one round/one executor. Pin exact lane/model/effective effort for
follow-ups. Empty admission blocks; never lower a floor, reroll, or fall back
across a billing boundary. Preserve visible pre-run review and explicit override
semantics from the current implementation (not the superseded mandatory blind UX).

## Implemented and tested in this preparation

- `omnigent/model_advisor_tb4.py`: strict v2 preferences and explicit v1 conversion;
  qualified route and versioned evidence contracts; immutable private round
  snapshots; task-only advisor request and strict JSON parser; pure assignment
  and replay validation; deterministic exact gate/selection; final route/evidence
  drift guard; derived review/audit with observed execution and usage left null.
- `web/src/model-advisor/tb4Editor.ts`: mode labels, literal-floor parsing, copied
  paid selections, mode-preserving draft changes, stale/invalid-choice validation,
  and separate paid-advisor usage notice. No React component or HTTP wiring yet.
- `tests/model_advisor/test_tb4.py`: 86 offline Python policy cases.
- `scripts/test-tb4-editor.mjs`: 10 dependency-free Node editor-contract tests.

Executed in an isolated source slice using Python 3.13.5 and Node 22.16:

```sh
PYTHONPATH=. python -m pytest tests/model_advisor/test_tb4.py -q
python -m py_compile omnigent/model_advisor_tb4.py
node --experimental-strip-types --test scripts/test-tb4-editor.mjs
tsc --strict --noEmit --target ES2022 --module ESNext web/src/model-advisor/tb4Editor.ts
```

These are **86 passed + 10 passed**, compilation and focused TypeScript checking,
NOT full repository/React/browser/database/provider acceptance. Full git clone was
unavailable in this environment; GitHub source/API inspection and local new-file
validation were used. Ruff, Pyrefly, repo lint/build, migrations, existing suites,
real tool isolation, host transport and production behavior have not been tested.
No real model scores, credentials, inference calls or production state were used.
All test model names and scores are synthetic.

## First action for ZCode: fetch/pull current GitHub state

Before editing, run `hostname; id; pwd`, inspect remotes and `git worktree list`,
then **fetch origin and pull/fast-forward this task branch**. Do not work from
this document's or a chat's SHA without checking current remote refs. Use the
branch's own clean worktree; preserve unrelated/dirty work and never reset/stash
another agent's changes. Verify `origin` is `Mortified2896/omnigent`, not upstream.
Compare current main and task head before continuing. Reconcile newly merged main
changes deliberately; do not blindly merge old feature branches.

Read current `AGENTS.md`, `CONTRIBUTING.md`, `docs/task-scoring-and-test-sessions.md`,
this document, and the current advisor implementation. Inspect HomeLab PR #64 and
its merged operations note before touching any related runtime assumptions. The
Codex upgrade/merged catalog from that prior task must not be undone. A saved
config or a model's self-description is not actual-provider execution evidence.

## Finish the integration, do not restart it

1. **Catalog/evidence adapter.** Extend the authenticated host catalog boundary in
   `omnigent/server/model_advisor_service.py` (`build_host_catalog`/`load_catalog`)
   for this opt-in policy. Preserve existing paid candidate IDs. The old adapter
   is plan-specific; do not relabel arbitrary rows as free. Obtain connection
   classification, availability and task capability constraints from trusted data.
   Reuse reviewed TB4 evidence/catalog work where suitable; inspect old draft
   #167 and `omniroute-customizations` #5/#18 as references, do not merge wholesale.
   The free-chat draft #178 is not proof of working free coding/tool dispatch.
   Preserve original runtime model IDs (including required prefixes); evidence
   alias mappings must be explicit and auditable. No effort interpolation or
   silent benchmark-version/slice replacement. Missing/ambiguous evidence excludes
   a route even at floor 0. No synthetic fixture score may enter a real catalog.
   Source harness and executor harness stay distinct. Cross-harness reuse needs
   a reviewed versioned transfer policy; otherwise block. Scores remain reported
   overall pass@1, not invented confidence bounds.

2. **Deterministic routing adapter.** Supply `FloorCatalog.priority_ids` from the
   existing frozen deterministic cost/quota/priority policy and record its version.
   Add a stable ID tie-break only where that policy needs one. The core deliberately
   does not invent the missing ranking policy or prices. Gate actual tools/context/
   vision requirements before catalog construction. Show unsupported/empty pools
   explicitly. After assignment, a route failure cannot select another candidate.

3. **Persistence and API.** Integrate the v2 policy through the existing
   `model_advisor_workflow.py`, `model_advisor_repository.py`, service and
   `server/routes/model_advisor.py`. Reuse the registered application engine/table,
   not a standalone SQLite/JSON store or constructor DDL. Version/discriminate v1
   and v2 envelopes so historical rounds stay readable. Scope preferences to
   authenticated owner + host + profile. Preserve ETags, defaults hydration, new-chat
   persistence and temporary per-round changes. Reserve a stable client logical
   submission ID *before* inference, reject payload changes under the same ID,
   atomically save both floors and draw before dispatch, claim one launch, and
   recover uncertain work without automatic retry/rerandomization. Pure helpers
   alone provide none of these durable guarantees. Do not serialize the v2 frozen
   type into the old v1 decoder accidentally. Authorize all IDs server-side.

4. **Advisor transport.** Extend `omnigent/host/advisor_call.py` and the host/frame
   plumbing for the new request/output schema. Use `frozen.advisor_input()` only;
   exclude preference/catalog/review objects from outbound data. Prove an empty
   effective tool surface before dispatch, not merely a read-only sandbox or a
   prompt saying no tools. Keep transport-specific billing/entitlement verification
   and the chosen advisor model/effort. Initially accept text-only tasks with equal
   context for both selectors; reject unsupported attachments rather than omit them.

5. **Composer/review.** Wire through `web/src/lib/modelAdvisorApi.ts`,
   `web/src/model-advisor/editor.ts`, `ModelAdvisorPanel.tsx`,
   `NewChatAdvisorSection.tsx` and the existing composer entry point. Reuse the new
   pure helpers. Execution pool toggle; current paid model/effort checkbox controls;
   separate advisor+effort; literal human floor; Save defaults; Get recommendation;
   visible two-floor review/assigned floor/admitted models/configured executor;
   explicit Run/Cancel/override. Validate exact lane and effort: no first-item or
   nearest-effort substitution. Do not let displayed temporary settings be ignored
   by the server. Mark visible recommendations/unblinded reviews honestly; humans
   configuring a catalog are not necessarily catalog-blind like the advisor.

6. **Execution/telemetry.** Reuse normal server session creation and exact-selection
   runner logic. Call `require_dispatchable` immediately before the launch claim's
   dispatch boundary. Retain original randomized assignment when a human overrides;
   log override/fallback/cancellation separately, and never bypass the assigned gate
   while claiming experimental adherence. Preserve round -> session -> thread/turn
   -> response binding, original and assigned floors, both admitted sets, settings/
   catalog/evidence/ranking versions, configured vs observed model/effort/connection,
   same-executor disagreements, advisor/executor usage, reveal/override state and
   existing human outcomes/comments/Do not score. Missing observed fields are null,
   not copied requested values, self-identification or the latest unmatched log.
   Compare selector policies within stable mode/catalog/ranking snapshots; outcomes
   alone do not identify an intrinsic causal effect of a TB4 score.

7. **Acceptance.** Run new tests plus existing advisor core/workflow/repository,
   API, host, migration, exact-selection and frontend suites. Add real application
   launcher integration, owner isolation, concurrent duplicate-submit/reconnect/
   crash recovery, saved/temporary settings races, arbitrary floor boundaries,
   no-candidate cases, exact billing boundary on every retry, and browser hydration/
   toggle/review/first-task/pinned-follow-up checks. Prove the INITIAL submitted task
   receives its answer; a later follow-up answer is not a substitute. Do not claim
   actual provider identity from model self-description or config alone. Run repo
   Ruff/format, Pyrefly, web lint/type/build and targeted pre-commit checks.

## Scope and final report

Continue this task branch, commit/push tested source, update its **draft PR**.
No merge, deployment, O2 promotion, installed-release/config/catalog mutation,
provider billing expansion, real paid inference, secrets/permissions changes,
automated scoring/training, or destructive cleanup is authorized by this handoff.
The earlier deployment task/permission is not an automatic rollout of this new
routing policy. Keep existing live paths unchanged. Use fixtures/isolated servers
for acceptance here; list remaining real-provider gates explicitly. For separately
authorized live tests, apply test labels at creation and preserve uncertain evidence.
Report exact branch/head, changed paths, tests actually run, remaining blockers and
how to exercise the feature. Do not count draft-skipped CI jobs as passed tests.
