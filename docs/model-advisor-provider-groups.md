# Provider-grouped Model Advisor — Codex RTX continuation

Status: integrated on `codex/advisor-provider-groups-20260923` for PR #188.
Prepared head continued from `c00571fff38880c3ba78521c1a0774ed7012274e`; the
inspected base was `3d3082b453efbad332b0763bd8d73d25d599bbe2`.
No deployment, merge, database migration, secret change, or O1/O2 mutation was
performed by this follow-up.

## Required behavior

Use text-only OpenAI / ChatGPT plan and GLM / Z.AI provider sections. Each section
collapses independently. Every qualified model is listed INSIDE it, including
unchecked models. No Add/manage-model dialog, provider logos, generated images,
or one giant row per model/effort/route. Per-model effort checkboxes wrap on mobile;
only actual supported effective levels appear. Multiple levels per model are valid.

The provider On/Off switch masks its remembered answer choices. Off does not clear
model/effort selections; On restores exactly those choices, not every catalog model.
Collapsing does not disable a provider. Saved defaults persist through the existing
owner/host/profile settings store. Newly discovered models are never auto-selected.

The separately selected advisor may use OpenAI or GLM regardless of answer-provider
switches. Those switches are explicitly labeled "Enable ... answers", not account
disable controls. Preserve the advisor choice while the answer pool changes.

The semantic treatment is (provider family, canonical checkpoint, effective effort).
OmniRoute and Direct are transport paths, not competing advisor candidates. Default
new connection preference: OmniRoute preferred, Direct fallback; Direct only is an
explicit alternative. Resolve transport AFTER the model/effort decision. Connection
policy applies independently to the advisor's own call and the selected answer call.

## Existing-source findings

`omnigent/server/model_advisor_service.py::_lane_classification` admits OpenAI
`codex-direct` rows, explicitly host-attested Codex OmniRoute aliases, GLM
`glm-direct`, and the qualified GLM OmniRoute vocabulary. Other gateway rows are
excluded. The OpenAI gateway aliases are accepted only with the host-stamped
provider/class/connection/entitlement tuple; model-name prefixes alone remain
insufficient evidence.

`model_advisor_core.Candidate` and v1 frozen rounds still bind route/account/model/
effort, and `build_advisor_request` sends `lane_id`. They are not reinterpreted.
The registered v2 path stores a versioned logical round alongside the existing
durable reservation, assignment, review, feedback and dispatch lifecycle. V1 decoding
and exact-pinned legacy execution remain unchanged; the advisor engine itself was not
rewritten.

## Integrated source

- `omnigent/model_advisor_provider_policy.py`: logical IDs, strict v2 preference
  serialization, provider masking, independent advisor lookup, loss-aware v1 mapping,
  route-free advisor input, exact qualified transport plans and safe fallback predicate.
- `web/src/model-advisor/providerPreferences.ts`: matching DTOs and pure grouped
  selection helpers; no localStorage authority or runtime requests.
- `web/src/model-advisor/ProviderSettingsPanel.tsx` and `provider-settings.css`:
  controlled accessible provider disclosure/switches, per-model effort chips, connection
  radios and explicit migration confirmation, independent advisor selector and Save.
- `omnigent/model_advisor_provider_workflow.py`, the existing advisor repository,
  API routes, host catalog adapter, real new-chat composer, and review surface now
  register the v2 logical workflow. v1 preferences and lane-pinned rounds remain
  on their original path.
- Python, TypeScript, and Vitest contract tests cover migration, provider masking,
  independent advisor selection, route-neutral input, exact dispatch labels,
  at-most-once claims, and safe/unsafe fallback evidence.

The logical catalog is supplied by a TRUSTED adapter. Do not build it by stripping
prefixes heuristically. Use explicit verified aliases: gateway `codex/...` and native
Codex slugs may denote one checkpoint; matching text alone does not prove equivalence.
Logical IDs contain no route, account, preference or availability. Removing/adding
transport paths must not duplicate candidates, change their IDs or rerandomize a round.

## Completed integration

The existing new-chat composer now hydrates host-scoped v2 settings, preserves the
existing human model/effort picker as the human proposal, and uses the provider
panel only for grouped answer settings, connection policy, and the independent
advisor engine. Save conflicts preserve the draft instead of overwriting it.

The server migrates v1 settings at read time with explicit catalog mappings,
retains unknown IDs and route-policy review state, and writes v2 only after an
explicit save. Historical v1 rounds are decoded and dispatched unchanged.
Live RTX inspection qualified the active Codex OAuth and GLM Coding Plan
connections without printing credentials. The live OmniRoute catalog exposed
`codex/gpt-6-astra`, `codex/gpt-5.6-{sol,terra,luna}`, `codex/gpt-5.5`,
`glm/glm-5.3`, and `glm/glm-5.3-flash`; direct GLM exposed `glm-5.3` and
`glm-5.3-flash`. Effort aliases were not invented: each route keeps the
host-reported supported levels.
Logical IDs contain only provider, canonical checkpoint, and effective effort.
The frozen advisor request contains no route, account, preference, human pick,
or fallback data. The full transport plan and per-attempt route are server-only
labels; the runner validates lane, model, effort, plan, connection, and route
before native startup.

The dispatcher records advisor and answer attempts. A typed
`PreDispatchRouteFailure` can use only a same-plan, same-checkpoint Direct leg
for the three explicit pre-upstream gateway causes; current binding is rechecked
before fallback. Generic errors, ambiguous timeout/502, quota, output, tools,
or an established thread remain uncertain/failed and are never replayed.

## Tests and release boundary

Focused backend and frontend checks passed:

- `tests/model_advisor/test_provider_policy.py`: 52 passed.
- `tests/model_advisor/test_provider_workflow.py`: 6 passed.
- `tests/server/routes/test_model_advisor.py`: 22 passed.
- `tests/runner/test_codex_native_launch_config.py`: 37 passed.
- Existing native/host regressions (`test_native_codex_provider.py`,
  `test_advisor_call.py`, `test_connect.py`): 251 passed.
- The provider-advisor component suite: 11 passed; TypeScript type-check,
  Prettier checks on changed frontend files, and Oxlint all passed.
- The production frontend build passed. Vite reported existing CSS `::highlight`
  compatibility warnings and large-chunk warnings; neither blocked the build.

The full frontend Vitest run was 7,079 passed, 38 failed, 3 expected failures and
1 skipped. The same 38 failures reproduced on the inspected base commit in the same
five unrelated chat/project-routing files, so they are baseline debt rather than
provider-advisor regressions. The provider-advisor tests remain green.

Rendered validation used regular Playwright with the installed system Chrome because
the Browser plugin was unavailable. A route-stubbed app run rendered the real new-chat
composer and provider settings at 390×844 CSS pixels: both OpenAI and GLM groups,
connection radios, effort chips, independent advisor select, and Save controls were
visible; document `scrollWidth` stayed at 390 (no horizontal overflow). A separate
desktop and unauthenticated mobile smoke screenshot also loaded the app shell. The
stubbed render was UI-only: it did not make provider calls or claim live launch
acceptance.

Live RTX inspection was read-only and qualified the active Codex OAuth and GLM Coding
Plan connections, current OmniRoute model vocabulary, direct GLM vocabulary, and
supported effort levels. No provider prompt, scoring chat, database mutation,
deployment, merge, or O1/O2 change was performed. The safe fallback and no-replay
claims are covered by synthetic typed-failure tests, not a production fault injection.

The branch is ready to push/update PR #188. Merge and deployment remain outside this
follow-up's scope.
