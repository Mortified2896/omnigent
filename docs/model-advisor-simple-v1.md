# Concrete Model Advisor v1 — isolated preparation

Status: draft, not integrated, not merged, not deployed. Owner request on
2026-09-20 explicitly forbids merging while the separate GLM repair is active.
That restriction supersedes earlier deployment authorizations in old handoffs.

## Scope and inspected baseline

Source baseline: `Mortified2896/omnigent@bd00321fb5fcea2e7b0185ab25623a8773387f0d`.
Read current refs before continuing; this SHA is evidence, not a future target.

PR #167 (`codex/task-success-forecast`) contains the earlier floor experiment:
independent proposals, 50/50 on disagreement, agreement at probability 1, and a
logical-attempt reservation. Use its behavior as reference only. Do NOT merge it
or import TB4/evidence-catalog, automated review, or broad routing work.
PR #171 (`codex/glm-access-lanes`) is a reference for GLM lane identity. The active
GLM repair in a different ZCode chat owns provider discovery, credentials,
Codex-native launch changes, and its worktree. Do not touch that worktree/ref.

Current `omnigent/server/task_experiment.py` provides append-only resource events
and human outcomes, with `response_link` mapping outcomes to logical attempts.
`docs/task-scoring-and-test-sessions.md` defines caller scope, excluded test
sessions and human-feedback privacy. Reuse these contracts, not another scoring
system. `experiment_item` stable IDs alone are not proof of atomic reservation;
verify uniqueness/transaction guarantees in the concrete store implementation.

## Product behavior

One initial submitted task is one round. The user writes the prompt and chooses a
concrete model plus effective reasoning level. The independently selected advisor
model sees the task and the SAME allowed answer choices, but not the user's pick.
Both proposals are frozen before execution. Different choices: server-side fair
coin, human 0.5 / advisor 0.5. Identical concrete lane/model/effort: `same`, 1.0;
never report this as a win for either chooser. 50/50 is a probability on each
disagreement, not a guarantee of exact alternating counts in a small sample.

Run ONE selected executor, not both. Pin its concrete route/effort for follow-ups
in that chat. Do not switch provider mid-thread on every message. A later explicit
new round starts a new bound task/thread with fresh choices; implicit evaluation
of every token, tool result, retry, or follow-up is out of scope.

Text-only initial tasks are the minimal v1. If attachments or extra context affect
execution, either provide both selectors the same qualified context and bind its
digest, or visibly reject that round as unsupported. Never silently choose from
an incomplete task. Preserve the actual execution instructions/permissions across
arms: this experiment varies selection, not execution privileges.

## Allowed choices and advisor settings

Use authenticated host model/capability discovery, not a hard-coded model list.
Limit candidates to the user's ChatGPT-plan Codex access and GLM-plan access.
Do not substitute usage-billed OpenAI API credentials, free-provider routing,
unknown gateways, wildcard model aliases, `auto`, or `custom/*` Combos.

A choice is the exact tuple `(lane_id, provider_id, connection_id, harness,
model_id, effective_reasoning_effort, access_class)`. Stable non-secret candidate
IDs are server-generated handles for those tuples. Use GLM repair's final generic
lane resolver; do not invent a parallel GLM client. Direct GLM and an existing
explicit concrete OmniRoute GLM lane, if offered, remain distinct. Never add an
OmniRoute Combo or silently fall between them. Reuse the normal Codex harness
where possible so comparing ChatGPT and GLM does not silently change harness.

Only advertise reasoning levels the selected lane/model actually supports.
Normalize aliases to effective provider values before presenting/freezing choices;
duplicate UI effort aliases must not become fake experimental treatments. Models
without effort controls use an explicit `not_applicable`, not ambiguous Default.

The advisor's own model AND effort are independently configurable. It need not
be in the answer pool; restricting answers to small models must not silently
change the advisor to a small model. Its route is subject to the same approved
ChatGPT/GLM entitlement boundary. No hidden fallback advisor.

Persist settings server-side per authenticated owner + selected host/profile:
- allowed concrete models and permitted effective efforts;
- advisor route/model/effort;
- enabled mode, initially `human_vs_advisor_50_50` when opted in;
- blind-review preference and version/revision.

Use the existing preference store if suitable; otherwise add a normal scoped
store/migration in the isolated test database. Do not add a standalone SQLite or
host-global JSON settings database. A localStorage cache is not the authority.
Store no credentials in preferences. Use revision/ETag conflict protection.

Defaults survive new chats, page reload and the same user reconnecting on another
client to the same server/host. Scope to host because credentials/catalogs differ.
No cross-O1/O2 shared database is required. Settings hydrate BEFORE submission;
never overwrite stored settings with an empty initial UI render. Per-round
changes are temporary unless the user presses Save defaults. Stale unavailable
choices stay visible but disabled with a reason; catalog refresh cannot silently
add models, change the advisor, or replace them with the first catalog entry.
Snapshot settings and the qualified common pool per round. Later defaults affect
new rounds only. An empty pool or missing advisor blocks submission explicitly.

## UI

Create additive components/hooks first, not a replacement for NewChatDialog:

    Model advisor: Off | Human vs advisor (50/50)
    Allowed answers: [Models and allowed effort levels...]
    Advisor: [Model + labeled access lane] [Effort]
    [Save defaults]
    Your pick: [Allowed model] [Allowed effort]
    [Run round]

Show saved versus per-round settings and unavailable-choice reasons. Integrate
with the existing composer only in THIS task's worktree, using a thin adapter to
the final GLM picker contract. Do not overwrite the GLM repair's files or runtime.

Preserve the previous blind-comparison intent: conceal the advisor recommendation,
assignment and actual executor identity until human feedback, or an explicit
Reveal action. Record early reveal as unblinded. Enforcement includes API/SSE
responses, browser state, model headers and nested transcript items, not CSS only.
This is metadata blinding, not proof that wording cannot reveal model identity.
After rating/reveal, show human choice, advisor choice, selected arm, actual route
and actual effort, short rationale and advisor overhead. Human outcomes remain
Success / Partial / Failed / Not sure, comments, tags and Do not score. No judge,
self-review, automatic outcome scoring, or training loop is enabled.

## Advisor transport and safe output

One bounded selection call using the user's chosen advisor; no recursive advisor,
research agent, paid meta-router, tool use or task execution. Build an allowlisted
input containing task + sanitized model/effort/capability descriptions only. Exclude
human pick, coin result, stored reviews/tags/comments, hidden conversation history,
credentials and future answer. Same visible task for both selectors; choose before
seeing any answer. The advisor returns strict bounded JSON with `candidate_id`
and a short rationale. Enum membership is validated server-side, including effort.
Malformed output, invented IDs, tool requests, timeouts and unavailable advisors
block the round. Do not silently run human choice and call it a randomized round.
A deliberate manual continuation is visibly outside the experiment and logged.

Use existing authenticated Codex access for ChatGPT-plan calls, not a generic
OpenAI SDK API key. An SDK/provider connection is not itself proof of subscription
billing. Authenticate/classify before exposing choices. Official references checked:
- https://developers.openai.com/codex/auth/
- https://developers.openai.com/codex/noninteractive/

An output schema or read-only sandbox does NOT eliminate inherited MCP/browser/
network tools. Prove pre-dispatch prevention and actual empty tool surface for
each advisor transport. If one supported advisor cannot be made tool-free, mark
it unavailable and finish mocks/other eligible adapters, without weakening the
safety boundary or blocking unrelated coding agents. Executor tool permissions
remain the normal selected task permissions; advisor-only isolation is separate.

Advisor calls consume their selected plan allowance too. Record advisor token
usage, reported cache counts, latency and errors separately from executor usage.
Null means unknown; do not invent dollar costs, quota remaining, cache hits or
capability scores. Use stable catalog summaries, not a new benchmark-sync system.

## Round state, atomicity and execution

Suggested lifecycle:
`draft -> human_locked -> advisor_pending -> choices_ready -> assigned -> running
-> completed/failed/cancelled`, with `blocked` for invalid/unavailable state.

Bind the original prompt and effective execution context hash, owner, host,
round ID, settings/catalog revisions, advisor identity, both proposals and
schema/policy version. Reserve an owner/host/logical-round key BEFORE calling
the advisor. Duplicate submit/reconnect must retrieve the existing reservation,
not launch another advisor call. Log explicit interrupted-advisor recovery;
unknown in-flight completion is not permission to retry invisibly.

Persist both choices, then draw using a server RNG, and commit the assignment
atomically BEFORE starting an executor. Concurrent requests may not generate two
assignments or two launches. Resume returns the same assignment. Changed payload
under an existing ID is a conflict, not a new coin flip. Client-supplied arm,
probability, existing Assignment or entitlement metadata is never authoritative.

Use durable existing workflow/dispatch mechanisms. A process-local lock and the
pure `prepare_assignment` helper are NOT sufficient across processes/restarts.
Test transaction/unique-key semantics and uncertain launch recovery. If the selected
route becomes unavailable after assignment, preserve the assigned treatment and
fail/block. Do not fall back, lower effort, switch account, or rerandomize. Manual
reselection is a separate explicit nonexperimental action/new linked round.

Persist requested and observed execution identity separately. Missing actual
model/effort provenance is unknown, never a copy of the request. Link the round
to the exact session and response before outcomes are joined. Do not infer a
binding from the latest log row. Preserve human revisions and exclusion semantics.

## What is implemented here versus still required

New pure module `omnigent/model_advisor_core.py`: immutable concrete candidate and
pool contracts, strict output JSON/enum parsing, independent outbound input builder,
round fingerprint, conditional 50/50 preparation, replay validation and exact-route
availability guard. No imports or changes in existing runtime modules.

New `tests/model_advisor/test_core.py`: offline unit tests. These establish local
contract behavior ONLY. They do not establish durable assignment, provider safety,
UI persistence, session creation or dispatch. Real server storage, authorization,
settings APIs, UI, provider adapters, event integration and gates still need work.

## ZCode continuation — NEW remote chat; draft only

Work in a NEW ZCode chat connected to RTX as `hermes`. Verify hostname/repo/remotes
and inspect `git worktree list` and current refs. Continue THIS prepared branch in
its own worktree, not the canonical checkout or the active GLM repair worktree.
Do not reset/stash/modify another agent's dirty work or push to its branch.

Use current AGENTS/CONTRIBUTING instructions. Do not merge #167, #171, #178 or this
branch; no main updates, deployment, release, O1/O2 restart, shared config/secret
changes, real provider calls, production DB writes, lockfile churn in another
worktree, or automated scorers. Keep the feature disabled in live systems. Mock
catalog and advisor/executor transports until the GLM repair publishes its stable
contract; later reconcile that contract in THIS branch only. Cap local test
parallelism while the other task is active; use independent venv, database, ports
and artifacts. Live GLM repair has priority; inability to test a live adapter is
an explicitly recorded pending dependency, not permission to touch it.

Finish source/storage/API/component work and offline integration tests, push the
branch and keep its PR DRAFT. Report tests actually executed, clean-main baseline
failures proved independently, pending GLM adapter integration and an exact future
integration checklist. Do not call skipped draft CI jobs passed tests.

Acceptance to add: actor isolation and preference persistence/new-chat hydration;
ETag races; common-pool equality; advisor independence and tool prevention; no
out-of-pool model/effort/lane; invalid/timeout handling; concurrent duplicate submits;
crash/restart replay with no second draw or launch; before/after assignment edits;
selected quota exhaustion; no cross-lane/API-billed fallback; response linkage and
feedback exclusions; metadata reveal enforcement; disabled-feature unchanged
behavior; GLM-repair regression suite after its contract is available.

Local contract checks used in this preparation (no full checkout available):

    PYTHONPATH=. python -m unittest discover -s tests/model_advisor -v
    python -m py_compile omnigent/model_advisor_core.py tests/model_advisor/test_core.py

Run repository-required lint/types and broader backend/frontend tests in the proper
isolated RTX environment. This preparation does not claim those were executed.
