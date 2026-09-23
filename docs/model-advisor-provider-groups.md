# Provider-grouped Model Advisor — Codex RTX continuation

Status: isolated source preparation, not connected to the live composer/API.
Base inspected: `3d3082b453efbad332b0763bd8d73d25d599bbe2`.
Scope: mobile settings, preserved provider selections, transport-neutral choices.
Do not merge/deploy this follow-up merely because earlier advisor rollout tasks
were authorized. Keep the current O1/O2 release and unrelated PRs untouched.

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
`codex-direct`, GLM `glm-direct`, and GLM-prefixed `omniroute` rows. Other gateway
rows are excluded. This explains Direct-only OpenAI entries in the current advisor;
it does NOT prove that RTX lacks an OpenAI subscription connection in OmniRoute.

`model_advisor_core.Candidate` and current frozen rounds bind route/account/model/
effort, and `build_advisor_request` sends lane_id. Do not silently reinterpret those
persisted rounds. Introduce a versioned logical round path reusing existing durable
reservations, assignment, review, feedback and dispatch lifecycle. Keep v1 decoding
and exact-pinned legacy execution. No rewrite of the already-working advisor engine.

## Prepared source

- `omnigent/model_advisor_provider_policy.py`: logical IDs, strict v2 preference
  serialization, provider masking, independent advisor lookup, loss-aware v1 mapping,
  route-free advisor input, exact qualified transport plans and safe fallback predicate.
- `web/src/model-advisor/providerPreferences.ts`: matching DTOs and pure grouped
  selection helpers; no localStorage authority or runtime requests.
- `web/src/model-advisor/ProviderSettingsPanel.tsx` and `provider-settings.css`:
  controlled accessible provider disclosure/switches, per-model effort chips, connection
  radios and explicit migration confirmation, independent advisor selector and Save.
- Python and Node contract tests. These modules are deliberately unregistered so the
  working release cannot gain a cosmetic connection switch with no backend enforcement.

The logical catalog is supplied by a TRUSTED adapter. Do not build it by stripping
prefixes heuristically. Use explicit verified aliases: gateway `codex/...` and native
Codex slugs may denote one checkpoint; matching text alone does not prove equivalence.
Logical IDs contain no route, account, preference or availability. Removing/adding
transport paths must not duplicate candidates, change their IDs or rerandomize a round.

## Implement next in the SAME branch on RTX

1. Fetch current Omnigent and HomeLab refs, read AGENTS/CONTRIBUTING, and continue this
   branch in its own worktree as hermes. Preserve `.zcodeignore`, other dirty work,
   active chats, the GLM repair and PR #183 TB4 work. No upstream push.
2. Review existing API/store integration and register v2 preferences/rounds. Reuse the
   application's database, CAS/ETags, authorization and submission-key reservations.
   Add a thin codec adapter, not another database/service framework.
3. Map old saved candidate IDs using explicit catalog evidence. Retain unknown IDs;
   collapse equivalent transport duplicates. Require explicit connection-policy review
   before applying new fallback behavior to legacy preferences. Do not auto-enroll or
   erase missing models. Old pending/completed rounds retain their old schema/route.
4. Wire ProviderSettingsPanel into the existing composer, not beside a duplicate old
   settings form. Preserve review/override/50:50/feedback behavior, host-scope race
   defenses, saved versus temporary settings, and new-chat/reload hydration.
5. Qualify actual RTX OmniRoute OpenAI/Codex subscription and GLM Coding Plan paths.
   Read actual active gateway connection identities, endpoint/model/effort mappings,
   current host catalogs and default-provider configuration. A model name, source label,
   catalog price or account-family constant is not proof of subscription entitlement.
   Do not substitute billed OpenAI API credits. Do not invent a gateway connection;
   missing credentials/admin changes require explicit owner action. No secret logging.
6. Canonicalize supported reasoning semantics BEFORE logical IDs. Reject unsupported
   levels or unproven equivalence; do not guess that Max/Ultra or Medium/High are equal.
   The exact same model/effort may have different wire spellings only when verified.
7. Freeze a private transport-policy snapshot separately from the logical pool and
   advisor input. Preserve both proposals, original draw and overrides. Advisor sees
   model/effort and neutral capability descriptions only: no routes, duplicate transport
   candidates, route-encoded IDs, human pick, retries, quota, account or preference.
8. Implement a scoped host dispatch resolver using QualifiedRoute attestations for the
   SAME checkpoint/effort/harness contract and SAME plan/account. Bind explicit selected
   connections; prevent gateway auto/Combo, emergency and account fallbacks escaping
   that contract. Do not globally alter OmniRoute routing for unrelated sessions.
9. Fallback only for a proven gateway failure BEFORE upstream execution. Unknown
   timeout/502, plan quota exhaustion, partial output, tool execution or an already-bound
   provider thread must NOT be replayed on Direct. Persist failed attempts and reasons,
   revalidate current binding before each dispatch, and never rerun the advisor/coin.
   Once a stateful execution starts, keep its physical route pinned; fail visibly if
   recovery cannot preserve its thread/context without duplicated effects.
10. Cover both advisor and answer transports, including actual empty advisor tool
    surface. Keep logical selection and requested/observed route+effort+attempt evidence
    separate. Show "via OmniRoute" or "Direct fallback: reason" without presenting
    route preference as part of the advisor's recommendation.

## Tests and release boundary

Executed here in a container SOURCE SLICE, not a full checkout:
- 52 Python pytest cases passed, all synthetic/no provider calls.
- 19 Node tests passed against strictly compiled providerPreferences.ts.
- 8 actual Chromium component scenarios passed at 320/375/390/430/768 CSS-pixel widths.
  The controlled fixture used bundled React 16.0.0 with the transpiled component,
  not the repository's React version. Save/remount used a fixture memory store;
  browser reload, authenticated persistence and live app integration are NOT proven.
- TSX syntax transpilation, Python compilation, 99-column Python source/test check,
  and Git diff whitespace checks passed. Canonical Ruff/Prettier/Pyrefly/full React
  type checks/Vitest/production build were unavailable and remain for RTX.
- Container git clone failed DNS. Publication uses the authorized GitHub connector.

Codex must run the canonical formatters on changed files first, then repository
unit/integration/browser checks. Add real service tests: v1 migration, paused-provider
save/reload, independent advisor, byte-identical advisor input on route changes,
no duplicate logical choices, exact reasoned model mapping, safe/unsafe fallback,
retry/crash/stream/tool boundaries, pinned follow-ups and all-attempt provenance.

Use an isolated app/DB and mock gateways first. Do not call broad historical CI debt
an introduced regression; prove any baseline claims on current main. Real RTX routing
qualification and narrowly bounded acceptance are the remaining host-local work;
report exact missing verification instead of widening billing or deleting state.

Push and update this draft PR. Stop before merge/deployment for this new follow-up.
Once separately authorized, deploy the same accepted artifact O1 first, then O2,
with migrations/settings compatibility and independent backups/rollback acceptance.
