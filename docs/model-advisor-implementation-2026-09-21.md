# Model advisor: approved UI implementation and RTX continuation

**Draft source only. No merge to main, deployment, live database change or
provider inference is authorized by this continuation.**

## Baseline and reuse

GLM PR #180 is merged at `01473d34b9a5d9810f3bed5311f6f10bfb0e9274`.
Old PR #171 is closed as superseded; do not reopen/reimplement its proxy.
Advisor PR #179's existing core, 27 original tests and initial design are
preserved. This work uses that core for independent prompt construction,
strict output validation, exact route checking and the default 50/50 draw.
The feature branch incorporates main with history preserved, not a force rebase.

Read this document after `docs/model-advisor-simple-v1.md`: **the visible review
and explicit override below supersede that document's mandatory blind-until-rating
UX**. The approved diagram is an interaction sketch, not evidence for hard-coded
GPT-4o availability, model-strength rankings or automatic learning from feedback.

## Concrete v1 interaction

1. Hydrate saved defaults for the authenticated user + host + profile.
2. The user selects the shared answer pool (exact model/lane/effort options),
   separately chooses the advisor model/effort, and optionally saves defaults.
3. The user writes the task and chooses their own model/effort. Get recommendation
   freezes that input and reserves the round before one bounded advisor call.
4. The advisor sees the task and the SAME allowed answers, not the user's pick.
5. Persist both proposals and the original assignment. Start at 50/50; the approved
   balance slider records the actual configured probability. Agreement is `same`,
   not a win for either side. 50/50 is not forced alternation or exact short-run counts.
6. Show both choices, brief rationale and original assignment BEFORE executing.
   The user confirms or explicitly overrides within the frozen pool, with a reason.
   Overrides retain the original assignment and are separately labeled. These are
   visible/unblinded runs; never present them as a blinded experiment.
7. Claim execution once, run ONE selected model, attach actual model/effort/route
   and response ID from execution evidence, then reuse the existing human feedback.

For this first version **one initial task is one round**, as in the prior brief.
Follow-ups keep its chosen route; a deliberate new round is explicit. The diagram's
"turn" label is not permission to swap providers silently inside a resumed Codex
thread. Text-only initial prompts are supported by this slice. Reject attachments
or additional task-affecting context until an equally visible, hashed context
extension for both selectors exists; never silently omit it. No auto-training,
self-review or scoring evaluator is introduced.

## Implemented source

- `omnigent/model_advisor_workflow.py`: validated preferences; stable route-based
  candidate IDs; immutable task/pool/settings snapshots; bounded advisor selection;
  original assignment with configurable balance; visible review; explicit override;
  exact current-availability recheck. Reuses `model_advisor_core.py` unchanged.
- `omnigent/model_advisor_repository.py`: SQLAlchemy persistence on a supplied engine.
  Versioned preference saves; owner/host/profile scoping; create-before-advice round
  reservations; transactional assignment publication; explicit one-winner execution
  claim; pre-execution cancellation. No constructor DDL or private DB files.
- `web/src/model-advisor/editor.ts`: controlled editor state, safe initial hydration,
  host-scope isolation, stale-response protection, save-vs-temporary settings semantics,
  labeled choices and validation. No automatic localStorage authority or saving effect.
- `web/src/model-advisor/ModelAdvisorPanel.tsx`: settings/pool/advisor/balance controls,
  human choice and Get recommendation; a separate review/override/Run/Cancel component.
  These are real React components but are not imported into the composer yet.
- Tests cover the new workflow/database primitives and pure frontend editor logic.

## What ZCode must integrate on RTX

Work in PR #179's OWN worktree. Fetch its current remote head; do not reset dirty
work or overwrite a newer agent commit. Preserve unrelated tasks. Follow current
AGENTS, CONTRIBUTING and task-scoring/test-session instructions. No production
mutation or use of the old GLM worktree. The existing GLM repair is now a main-line
dependency, not an active unmerged feature to cherry-pick again.

### Store and authenticated API

Inspect the existing application database/settings conventions before wiring.
The repository primitive receives the normal application SQLAlchemy engine. Its
standalone MetaData is an unregistered table definition: port/register it through
the project's reviewed schema/migration mechanism, not runtime create_all. Tests
use create_all ONLY in disposable fixture databases. If an existing generic
versioned-record table is suitable, adapt the storage backend rather than making
a parallel settings system. Current tests cover SQLite, not production
PostgreSQL/MySQL migration behavior; validate all supported backend targets.

Implement caller-authorized endpoints under the existing routing conventions for
loading/saving preferences, creating/polling rounds, confirmation/override/cancel
and inspection. Derive owner from authentication, verify host/profile permissions,
use If-Match/versions for saves and confirmation, and never trust a client-supplied
Candidate, assignment, random draw, owner, entitlement or actual-model report.
A profile defaults key must never be global across users or hosts.

Do not return the raw private Record payload to the browser. It contains the
private frozen snapshot. Project only the needed UI fields: review fingerprint,
human/advisor/assigned candidate IDs, assigned arm, rationale, balance and status.
The React ReviewView uses those projected IDs rather than the internal nested
Assignment object. Keep credentials out of every payload/log and treat credential
fingerprints as private cache identifiers, not UI connection IDs.

The workflow currently blocks a round containing a stale saved answer choice.
The UI displays it with a removal action; it is never replaced automatically.
Defaults changed after reservation do not affect that round's private snapshot.

### Real catalog and transport adapters

Reuse main's `resolve_native_codex_catalog_launch`, lane metadata and actual
capability discovery. Exact supported lane IDs include `codex-direct`, `glm-direct`
and, only if deliberately authorized for GLM-plan use, `omniroute`. Do not infer
ChatGPT-plan access from a model author's name or from an unlabeled default gateway.
Normalize equivalent provider/model/effort spellings BEFORE stable candidate IDs.
Keep lane/account distinctions and effective supported efforts. Do not auto-enroll
new catalog entries into the user's saved answer pool.

The advisor's selected model may be outside the answer pool, but must still be an
authorized ChatGPT-plan/GLM-plan option. One bounded, tool-free recommendation only.
Prove actual zero model-callable tools across inherited MCP/browser/agent/tool
paths for its transport. A prompt saying "no tools", output JSON schema or read-only
sandbox is not enforcement. Reuse normal authenticated Codex-plan transport;
never substitute a separately billed OpenAI API key. No hidden advisor fallback.

`reserve_round(...).acquired` is the only permission for this logical request to
start its advisor call. Duplicates poll existing state. The raw result is processed
internally through `finish_advice`; errors/timeouts must be recorded as blocked,
failed or explicitly uncertain. Add bounded provider timeout/output budgets, usage
and latency capture, cancellation forwarding and intentional recovery paths.
The primitive deliberately does not implement those network lifecycle callbacks.

`confirm(...).acquired` is the only permission to launch the selected executor.
It supplies a stable dispatch ID. Persist the exact session/response binding and
actual provenance, finish/fail/cancel/unknown records and inspection projection.
Do not guess bindings from the latest log row. A crash after claiming is not proof
that no provider request occurred: use durable dispatch reconciliation or visibly
stop for inspection; never replay blindly. This is an at-most-once claim primitive,
NOT an exactly-once provider-execution guarantee.

**Critical:** current main's ordinary runner can replace an unsupported explicit
model with the lane's default (`omnigent/runner/native/orchestration.py`, catalog
validation/reset path). The advisor round MUST bypass that replacement behavior
through a scoped, enforced exact-selection policy. If the chosen model/effort/account
becomes unavailable, fail that assignment, do not reset it even within the same lane.
Do not alter fallback defaults globally for normal GLM/Codex sessions. Test the
actual runner launch, not only the pure helper, for this invariant.

### UI and feedback

Wire the controlled components into the existing composer under an OFF-by-default
feature gate; API gating must match. Parent code owns async calls, scope changes,
version errors, prompt availability and proper server DTO decoding. No provider
call should start on keystrokes or component mount. Do not offer fixed model names
from the diagram; use only the verified account catalog.

Use the project's canonical formatter/lint/type checks before finishing this
branch. The new TSX has only parser validation here: run real React/Vitest tests
and an isolated rendered browser walkthrough. Add tests for double click,
loading/failed settings, host switches during requests, saved effort restoration,
late save completion, preview-vs-Run separation, overrides and unknown provenance.
Reuse existing feedback/outcome/Do not score behavior; advisor calls must not see
stored human reviews. Record the original draw, actual executed choice and override
separately so users can inspect both assignment and execution honestly.

## Validation performed here

- 44 new Python unittest cases: workflow logic plus real SQLAlchemy/SQLite fixture
  persistence, reopening the database, CAS conflicts and concurrent reservation /
  assignment. Provider-free; no Omnigent app import/runtime or network execution.
- 13 Node node:test scenarios against the STRICT-TYPESCRIPT-COMPILED editor module.
  The committed Vitest file carries equivalent cases, but Vitest itself did not
  run in this container (no project node_modules).
- Python compileall passed. React TSX parse passed (syntax only, NOT a React render
  or React type check). Core source was copied from GitHub and its Git blob hash
  matched the original `7cab851acf07002d0b248531e9b278a5d5945c17`.

Full repository tests, canonical Ruff/Prettier/Pyrefly, HTTP auth integration,
production backend migrations, SDK calls, durable external dispatch and browser
acceptance remain for RTX. Do not call any skipped CI job a passed test. Fix only
introduced regressions; reproduce claimed baseline failures on the exact base.

After integration push THIS feature branch, update #179 and keep it DRAFT. No
merge, deploy, O1/O2 restart, secret changes, live provider calls, migration of the
live database, AI scoring enablement or branch/evidence cleanup in this phase.
