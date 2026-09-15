# Mac O3 lazy tool discovery: implementation and acceptance

The final installed local backend reduced a fresh greeting from **89,061 to
16,285 input tokens** (81.7%) while retaining **539 registered MCP tools**.
An ordinary coding request dropped from **88,075 to 16,477**, on the same
resolved Terra model. Tool-attributed input fell from **78,055 to 7,203** for
the greeting. These are native-session measurements, with separate provider
attribution replays; unit tests alone are not the acceptance evidence.

## Implementation-level root cause and ownership

O3 generates `custom/o3-route-*` Combo aliases. Native Codex builds the initial
request before OmniRoute resolves a destination. The aliases were absent from
the selected model catalogue, so Codex's unknown-model behavior eagerly exposed
specialist schemas. The existing catalogue-extension path was gated to Smart
Routing and deliberately left an explicit user catalogue untouched. Neither
path supplied capability metadata for these O3 aliases.

`omnigent/server/o3_routing_review/tool_search.py` now owns the O3 capability
contract. It combines explicit, exact provider/route qualification records with
the installed Codex model catalogue, checks the local gateway URL/version, and
intersects support across **every** primary/fallback target. Nested, unknown,
unqualified, or vendor-revoked destinations cannot inherit another model's
search capability. A gateway URL/version change requires requalification.
External fallback-chain metadata must be empty to establish a closed target
set; unknown external fallback chains disable the alias search advertisement.

`CodexNativeAppServer.start()` invokes this before starting the native client.
It writes a private merged catalogue and passes its path as a Codex config
override. The global catalogue, original entries, MCP registration, credentials,
and provider config are preserved. A stale private alias advertisement is
removed when its complete target set cannot satisfy the capability contract.
The alias keeps the gateway's negotiated context window. Other boolean
`supports_*` fields are intersected too; code-only tool mode is not forced.
The deterministic first qualified vendor descriptor supplies the client prompt
format, accounting for some difference from the earlier temporary diagnostic.

At execution approval, the local qualification policy excludes unqualified
routes and records explicit execution-set exclusion reasons. Adviser rankings,
benchmarks, estimator selection, recommendations, and ranking weights remain
unchanged. Selection and fallback continue across the qualified subset. This
is an intentional capability constraint, not a claim that all original
providers support client tool search.

## Destination matrix

This is the complete 13-route execution set of the active diagnostic proposal,
read back from the local O3 API. Eligibility is task/floor dependent; this is
not a claim that all possible future O3 tasks have this set.

| Provider | Exposed route | Previously eligible | Client search + loaded call | Final search-dependent execution |
|---|---|---|---|---|
| codex | `codex/gpt-5.5` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.3-codex-spark` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.5` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.5-low` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.6-luna` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.6-luna-low` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.6-sol` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.6-sol-low` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.6-sol-ultra` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.6-terra` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.6-terra-low` | Yes | Passed | Eligible |
| codex | `cx/gpt-5.6-terra-ultra` | Yes | Passed | Eligible |
| openrouter | `openrouter/minimax/minimax-m3:free` | Yes | Unknown: free route returned 404/unavailable, then cooldown | Excluded |

The persisted MiniMax Combo target omits the outer `openrouter/` prefix; it is
still checked with its explicit provider identity. Every Codex row was probed
through the local gateway, first emitting `tool_search_call`, then receiving a
`tool_search_output` containing a diagnostic function, then invoking that
function with the exact expected arguments. Resolved model identities came
from actual responses, not suffix/name inference. Vendor `supports_search_tool`
is checked again when the private catalogue is produced. All admitted targets
can participate in existing routing/fallback selection. No native search
capability is established here for adviser-only free-provider routes, which
remain separate from the native execution policy.

## Small core, lazy specialists, and activation

The final fresh native request has 14 top-level tool entries: command execution,
process input, MCP resource listing/templates/read, user input, plugin install
request, patch application, image viewing, get/create/update goal, generic tool
search, and native web search. These are the installed Codex client surface;
this change does not implement a second tool broker.

OmniRoute, Omnigent specialist operations, GitHub, Gmail, Google Drive, calendar,
Todoist, Anki, shadcn, and other MCP/app operations remain registered and
searchable. **No OmniRoute specialist schema is eagerly present** in the fresh
greeting or coding request.

The routing control plane is ordinary application HTTP code: model/Combo
inspection, approval-time Combo creation, and startup capability checks. It is
outside the model's tool context. Routing through OmniRoute does not require
exposing its 110 specialist MCP schemas to the model.

The final OmniRoute task searched for health inspection, loaded **5 of 110**
OmniRoute tools plus three incidental Todoist search matches, then successfully
called `omniroute_get_health`. The result reported local OmniRoute 3.8.50 health.
An earlier successful run requested five matches, loaded two OmniRoute tools
plus three Todoist matches, and continued across Spark and Terra destinations.
The native BM25 search is granular but can return imperfect matches; this
implementation does not claim that every returned match is necessary.

## Fresh local Mac telemetry

| Case | First input | First cached input | Final current context | Cumulative input, separate | Window | Resolved model |
|---|---:|---:|---:|---:|---:|---|
| Original minimal baseline | 89,061 | 3,328 | 89,061 | 89,061 | 258,400 | Luna |
| Final minimal | 16,285 | 3,328 | 16,285 | 16,285 | 258,400 | Terra |
| Before coding | 88,075 | 3,328 | 99,043 | 187,118 | 258,400 | Terra |
| Final coding | 16,477 | 9,472 | 18,131 | 34,608 | 258,400 | Terra |
| Final OmniRoute inspection | 16,484 | 9,472 | 19,009 | 54,247 | 258,400 | Terra |
| Unqualified-primary fallback diagnostic | 91,203 | 3,328 | 91,203 | 91,203 | 258,400 | Spark |

The original baseline is the previously captured local diagnostic supplied with
this task. All other rows were captured during this implementation. The coding
fixture exports a greeting function; the prompt asks to read and explain it,
without file changes. The final coding run invoked only `exec_command`, with
no tool search or OmniRoute specialist activation.

Native sessions had unique thread IDs, fresh private homes, no fork provenance,
and no prior assistant messages in their first requests. No adviser/history
leakage was found. Cache counts are separate and are never subtracted from
context occupancy. Final cumulative total tokens (including output) are 16,291
for minimal, 34,757 for coding, and 54,418 for OmniRoute inspection.

The final minimal cost is in the 14–16k diagnostic class rather than 89k. Earlier
successful coding/discovery runs used 14,821/14,820 first-input tokens. Model
rendering and negotiated descriptor differences prevent exact equality with
the original temporary 14,483-token descriptor control.

### Tool attribution and registry evidence

Read-only native `mcpServerStatus/list` comparisons and the live acceptance test
proved identical complete tool-name sets: **539 before and after**, including
**110 OmniRoute** and **37 Omnigent** tools.

Provider attribution replays preserve the native first-request contents:

| Payload | Input tokens | Tool-attributed input | Replay model |
|---|---:|---:|---|
| Original minimal baseline | 89,061 | 78,055 | Terra |
| Before coding | 88,075 | 78,055 | Terra |
| Final minimal | 16,285 | 7,203 | Terra |
| Final coding | 16,477 | 7,369 | Terra |

The final replays reconcile exactly with their native input counts. They are
attribution controls, not substitutes for the native runs. Complete sanitized
usage fields and request tool inventories are in [the evidence fixture](../../tests/fixtures/o3_tool_search_20260909.json).

## Fallback and accounting

A separate local priority Combo had an unqualified diagnostic primary and a
qualified Spark fallback. Its native alias did **not** advertise search. The
request completed on the `qualified-fallback` step, verified in gateway logs.
Its high schema cost is intentional conservative behavior for an alias whose
complete destination set is not qualified. No real provider credential,
cooldown, or existing route was modified to provoke this diagnostic.

The capable routing test also exercised continuation after discovery across
Spark and Terra. Both destinations had independent qualifications, so the
second destination did not inherit an unsupported search contract.

The existing accounting correction is retained: only `tokenUsage.last.inputTokens`
sets current context; cumulative input never substitutes when `last` is absent.
`modelContextWindow` is read independently. The stale total-only live-frame test
now asserts cumulative updates without inventing context occupancy.

## Verification and installed artifact

- 372 relevant native/catalogue/routing/accounting tests passed.
- The read-only native end-to-end test passed against four actual local Mac
  sessions, comparing MCP registries, request schemas, usage, and discovery.
- All 12 eligible Codex destination protocol probes passed.
- Pyrefly passed with zero errors.
- The installed Mac app visibly showed a completed fresh greeting and 6% context.
- The local backend was gracefully restarted after sessions were idle, and
  recovered on the same loopback port with health `ok`.
- Required all-files pre-commit was attempted. It found pre-existing HomeLab
  formatting/unused-variable failures and a VS Code TypeScript hook failure.
  Unrelated hook-made formatting was restored; it is not part of this change.
  All scoped pre-commit hooks passed, including Pyrefly and the model-ID lint.

The installed runtime is `/Applications/Omnigent.app`, with its existing
`app.asar` SHA-256
`b087afb6b7050f5f3814fd5254e7da8869227d9d7278685ce2bf7665efc872b5`.
It uses the stable Mac integration checkout's editable Python environment.
Only the relevant backend modules were installed there; original copies and a
before/after hash manifest were retained. The matching backend wheel was built
as `omnigent-0.9.0.dev0-py3-none-any.whl`, SHA-256
`7ca6c433b7f8ce245315e5414c9eb81061917391bf490af363181dcd0463a47a`.
The wheel itself was not installed: acceptance exercised the installed app's
editable backend with the matching module contents.

O3 validation was performed against the local Mac Omnigent/Codex environment.
O1/O2 server instances were not used as substitutes and were not modified.

## Reproduce and operate

1. Keep the normal MCP configuration. Use the installed local O3 app and approve
   a new routing proposal; new search-dependent routes use the qualified set.
2. Start a fresh native session in an empty directory and send `hi`. Expect a
   roughly 14–16k first-input class and about 6% of the unchanged window.
3. Start a separate fresh session with a small source file and ask it to read
   and explain the file. OmniRoute schemas should remain absent.
4. Start another session and ask it to discover and call the local OmniRoute
   health tool. Inspect the search output and successful tool result.
5. Run the live test with the four session IDs:

```sh
O3_EAGER_SESSION_ID=<before-session> \
O3_LAZY_SESSION_ID=<fresh-greeting-session> \
O3_CODING_SESSION_ID=<fresh-coding-session> \
O3_DISCOVERY_SESSION_ID=<fresh-health-session> \
uv run pytest tests/e2e/test_o3_tool_search.py -q -p no:rerunfailures
```

The operator-owned registry defaults to
`~/.omnigent/o3/tool-search-capabilities.json`; override it with
`OMNIGENT_O3_TOOL_SEARCH_CAPABILITIES`. Generate a new reviewable registry with:

```sh
uv run python scripts/o3_qualify_tool_search.py \
  --alias <approved-alias> --output <new-qualification-file>
```

The qualifier issues explicit synthetic protocol probes only when invoked. It
never runs on ordinary user requests, changes credentials, or edits routes.
Review the results before installing the new registry. Preserve the previous
file for rollback. Requalify after gateway/adapter/model-mapping changes.

## Limits and remaining risks

- Existing mixed or unqualified aliases remain conservative/eager. Create and approve
  a fresh routing proposal to obtain a new qualified execution route; old aliases and sessions
  are not silently rewritten or deleted.
- MiniMax free-route search support is unknown because it was unavailable.
  Other future providers require their own qualification and truthful client
  descriptors. Model names do not establish support.
- The gateway URL/version and installed vendor metadata are checked at startup.
  Same-version adapter changes still require operator requalification. External
  edits to an already running owned Combo are outside this startup contract;
  do not change its target set behind an active native session.
- Initial coding/discovery attempts hit a transient upstream connection timeout
  and route availability filters. Fresh retries passed; network/VPN settings
  were not changed.
- Full-repository pre-commit is not clean at the existing base. The unrelated
  HomeLab/VS Code failures were kept out of this Mac fix.
- Native search may return a small number of unrelated matching tools. It does
  not load an entire OmniRoute family simply because one tool is needed.
