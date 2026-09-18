# Local Mac O3 free-execution investigation — 9 September 2026

**CAPABILITY LIMIT for the tested current native execution path. No additional free route was qualified, so no production admission rule or capability declaration was weakened.** This does not prove that every unprobed free provider is intrinsically incapable. It establishes why the observed set was narrow, and why the tested broader routes cannot currently be admitted safely.

The installed local service at `127.0.0.1:6768` and local OmniRoute 3.8.50 at `127.0.0.1:20128` were used. O1/O2 were not accessed or modified. No merge, release, deployment, backend replacement, credential change, subscription assumption, or VPN change was made.

## Why MiniMax was the only non-Codex candidate

The 13 destinations were **not the initial candidate pool**. The old proposal evaluated 3,170 route IDs. The new live snapshot contains 3,174 IDs, including 67 Combos; the attached matrix covers all 3,107 non-Combo route entries across 21 provider keys. Alias entries are retained, not counted as distinct underlying models.

The first capability gate is `omnigent/server/o3_routing_review/eligibility.py:52`, `build_execution_set`. It evaluates the union of all persisted forecast route IDs and current exposed IDs, then requires conservative benchmark evidence, approved reasoning configuration, required input/output modalities and limits, tools/structured output when requested, and Responses/adapter compatibility. `recommendation.py:136`, `recommend`, invokes this independently of its capped presentation rankings. There is no arbitrary MiniMax-only execution allowlist in these modules.

For the previous easy/low proposal:

- Common floor was 20. The original 3,170-route set had 3,116 decisions carrying unresolved Responses-contract compatibility; 1,204 each had unknown input and output modalities. Exclusion categories overlap.
- `opencode/big-pickle` and `oc/big-pickle` had recorded Responses callability and a conservative score of 20, but **both input and output modalities were unknown**. That is their first blocking gate, before native search qualification.
- Other routes have different combinations of missing modality evidence, unresolved Responses compatibility, incompatible reasoning configurations, and insufficient conservative scores. The matrix records each route separately.
- `openrouter/minimax/minimax-m3:free` happened to clear these persisted metadata checks. It was not evidence of a current MiniMax subscription, guaranteed availability, or native-search support.
- PR #149's **separate approval-time** gate in `service.py:694` removes unqualified native-search destinations. MiniMax is removed there. The fresh approved easy sets contain 12 Codex routes, not 13 executable routes.

The native UI still shows the pre-approval distinction imperfectly: its existing proposal can say “Leading option: MiniMax” alongside “Availability unverified.” That is not proof that MiniMax can execute. The approval-time evidence is authoritative for the created Combo.

## Exact routing flow

1. `service.py:190`, `create_proposal`: task and workspace summary enter estimator/adviser processing.
2. `service.py:238` and `:341`: live exposed model IDs are read. Estimator/adviser selection remains separate from native execution.
3. `service.py:347` → `recommendation.py:136`: benchmark floor maps into the common conservative capability floor.
4. `eligibility.py:52`: every forecast/live route is evaluated; no display cap limits the execution set.
5. Proposal returns pre-native-qualification capability decisions and resource advice. Cached forecast/readiness evidence is not a live provider guarantee.
6. `service.py:635`, `decide_proposal`, refreshes live IDs and recommendations at `:683`.
7. `service.py:694` → `tool_search.py:38`, `qualified_route`: exact provider/route search evidence is required, including for an estimator-labelled tool-free task.
8. `service.py:748` onward: readiness shortlist is rechecked; `omniroute.py:432`, `create_catalogue_combo`, constructs the owned route with the complete approved target set.
9. The Combo retains automatic health/stability/quota/cost/latency/session-availability weights and fallback. Cost weight is 0.15; current policy is not an unconditional “free always wins” rule.
10. `tool_search.py:54`, `combo_capability`, intersects qualification and installed client descriptors across every target. Native startup prepares a private alias catalogue before request construction. An unknown fallback cannot inherit search capability.

No production code contains a special MiniMax branch in this routing-review package. No paid MiniMax route was probed or substituted. Its free route returned **404: unavailable for free**, with a suggested paid alternative that was deliberately not used.

## Current inventory and capability evidence

See [provider-matrix.csv](provider-matrix.csv) or [provider-matrix.json](provider-matrix.json) for all 3,107 entries. Columns include provider/route, live presence, current and historical cost classification, account/auth requirement where exposed, active connection, context windows, live and forecast modalities, declared tool/structured-output support, conservative score, adviser applicability, observed Responses result, native-search result, cooldown snapshot, execution eligibility, and explicit exclusions.

The current management catalogue does not supply authoritative per-account billing entitlement or complete capability metadata. Therefore the matrix distinguishes exact zero pricing, free-labelled routes, provider-level “OpenCode Free” labelling, priced routes, subscription routes, and unknown cost. It does **not** treat every OpenCode model as independently proven free merely because its provider label says Free. Nor does “active connection” mean a successful request.

| Current catalogue classification | Entries |
|---|---:|
| Codex subscription | 81 |
| Route labelled free | 34 |
| Provider labelled free; individual entitlement unverified | 202 |
| Exact zero input/output price | 26 |
| Priced | 128 |
| Unknown | 2,636 |

Current catalogue provider-entry counts: OpenRouter 1,053; Requesty 1,099; OpenCode 218; AI Horde 240; Devin CLI 137; Codex 81; NVIDIA 59; Mistral 50; Auggie 28; TheOldLLM 26; Codex app-server 26; Cloudflare Playground 20; Groq 18; Cloudflare AI 18; Zcode 13; DuckDuckGo 6; Felo 5; VeoAI 4; UncloseAI 3; Zylo 2; Chipotle 1. This is an inventory, not a claim that all are free or inference-ready. Non-text and priced models remain in the matrix to make exclusion transparent.

### Lightweight direct probes

[probe-results.json](probe-results.json) records **66 route probes**, including aliases, using exact routes rather than a broad Combo. Discovery used tiny text requests, followed on HTTP-200 Responses envelopes by the same initial native `additional_tools`/client-search request shape used by #149. No route reached a successful search call, so none progressed to loaded-function or post-function-result qualification. No search capability was added.

The tiny text request had a 24-output-token budget. **18** probes returned nonempty response envelopes, **8** returned visible output text, and **3** returned exactly `OK`. Reasoning-only/truncated envelopes are not counted as useful visible answers. Failure under this tiny budget alone does not establish that a model cannot answer text with a larger budget. Search probes were separate requests without that 24-token ceiling.

| Group | Current observation | Native admission |
|---|---|---|
| OpenCode Big Pickle, both aliases | Visible `OK.`; native search not invoked | Excluded; missing forecast modalities and unqualified search |
| OpenCode Nemotron 3 Ultra free | Visible `OK`; native search not invoked | Excluded pending native contract and other row-specific evidence |
| OpenCode Ling/MiMo free | Responses envelopes, no visible text in tiny probe; search not invoked | Excluded; not established as native-compatible |
| OpenRouter Gemma 4 31B/26B free | Visible `OK`; 31B did not search, 26B omitted completed search response | Excluded |
| OpenRouter Laguna S free | Visible `OK.`; search not invoked | Excluded |
| OpenRouter Nemotron Lightning/Super free | Visible reasoning prose instead of exact answer; search not invoked | Excluded |
| Other OpenRouter free probes | Mix of envelope-only responses, empty responses, timeout, unavailable | Excluded/unqualified; per-route evidence attached |
| MiniMax M3/M2.7 free | 404 unavailable for free | Excluded; no subscription assumed |
| Cloudflare Playground | Missing local Playwright Chromium headless executable | Availability blocked before native qualification |
| DuckDuckGo | HTTP 418 in tested routes | Availability blocked; tool compatibility not established |
| NVIDIA exact-zero-price text candidates | Maverick rejected by active catalogue; GPT-OSS 120B returned 410 end-of-life | Unavailable |
| Mistral Leanstral zero-price entries | Read timeout | Availability unresolved; not qualified |

Other catalogue providers and unknown-priced routes were enumerated, not blindly probed. No material paid discovery sweep was performed. Per-account free quotas/credit-card requirements that the supported local API did not expose remain unknown, not fabricated.

## Adviser-only versus execution

Catalogue adviser applicability means benchmark evidence is applicable; it does not prove current adviser availability. A text response does not prove estimator JSON compliance, useful coding quality, ordinary function calling, or Codex client-search continuation. The matrix distinguishes these facts.

**Additional native free providers admitted: none.** The tested routes either failed before native protocol qualification or failed the initial native client-search stage. No model was marked search-capable based on its name or ordinary tool-calling metadata.

**Tool-free execution:** the estimator can return `tools: false`, but this does not disable Codex's tool surface or create an enforced tool-free runtime. The installed native bootstrap still registers the full tool set. Bypassing qualification on that flag would be unsafe. A separately enforced tools-disabled execution class could expand text-only usage, but implementing a new harness class is not a small candidate-filter correction.

**Tool-dependent execution:** expansion needs successful initial native search, search-output continuation, discovered function call, post-result continuation, suitable client descriptor, sufficient context/quality, current access, and truthful fallback metadata. No newly tested route satisfied even the initial search stage. A translation layer or provider-native support may help, but neither was assumed or implemented here.

## Fresh installed-Mac routing decisions

[routing-decisions.json](routing-decisions.json) contains classifications, estimator records, floors, candidate lists, resulting Combos and resource advice. [native-telemetry.json](native-telemetry.json) contains first/last usage, exact native threads, alias qualifications, complete registry hashes and matched gateway provenance.

| Task | Classification / common floor | Before native gate → approved | First input / cached | Actual first model | Result |
|---|---|---|---|---|---|
| Uppercase text | Easy, low / 20 | 13 → 12 Codex | 16,468 / 3,328 | Codex GPT-5.6 Terra | `HELLO WORLD` |
| Read/explain greet.ts | Easy, low / 20 | 13 → 12 Codex | 16,480 / 9,472 | Codex GPT-5.6 Terra | Completed |
| Discover local OmniRoute health | Easy, low / 20 | 13 → 12 Codex | 16,483 / 3,328 | Codex GPT-5.6 Terra | Search and health function invoked; completed |
| Demanding transaction-protocol task | Hard, high / 80 | 9 → 5 Codex | Unavailable | No completed request provenance | Stream closed before `response.completed` |

The demanding proposal asked for repository implementation; its native acceptance prompt was deliberately bounded to a read-only design response using that approved high-floor alias. No repository implementation or deployment was launched as a diagnostic. That native run failed, so its execution acceptance remains incomplete.

All four fresh native clients independently returned **539 registered tools** and the same complete registry hash. Successful runs retained a 258,400 effective context window. Current context uses latest-request input; cached input is reported separately and is not subtracted. No new free destination was added, so these are fresh preservation measurements, not “mixed free-plus-Codex after expansion” measurements.

The 12 easy approved routes are `codex/gpt-5.5`, `cx/gpt-5.3-codex-spark`, `cx/gpt-5.5`, `cx/gpt-5.5-low`, and the Luna/Sol/Terra routes listed in summary.json (low/default plus qualified Sol/Terra ultra). The hard set is `codex/gpt-5.5`, `cx/gpt-5.5`, `cx/gpt-5.3-codex-spark`, `cx/gpt-5.6-sol-ultra`, `cx/gpt-5.6-terra-ultra`. All retained targets remain subject to existing runtime routing and availability behavior.

The Mac UI visibly showed the text result and 6% context. It also displayed a `runner_disconnected` warning after completion. Later read-only inspection found the session idle and its native registry reachable. That warning is retained as an acceptance caveat rather than hidden.

## Tests, preservation and remaining gaps

- **58 passed:** routing review plus native search capability tests.
- **8 passed:** catalogue recommendations, including capability/effective-I/O eligibility.
- Existing tests cover non-Codex adequacy, threshold authority, no automatic floor lowering, wait/resume, uncapped candidate inclusion, strict Combo subsets, unknown/nested fallback rejection, vendor capability revocation, and gateway binding.
- The legacy four-session native test failed because its historical eager-baseline WebSocket at port 51233 refused connection. It did not reach the assertions. Fresh registry equality, first-input telemetry and specialist calls were independently captured instead. This is not reported as a passing legacy test.
- No real qualified free-native primary exists in these results, so a real free-primary → free/Codex fallback and a free-winning native tool-free task could not be demonstrated. Existing synthetic tests are not substituted for those live scenarios.
- The high-floor native stream failure remains unresolved. No quality floor was reduced to make a diagnostic pass.
- Cached catalogue/readiness metadata is dated September 4–5. Unknown modalities and stale availability remain real data-quality limitations. Merely filling those fields would still not qualify the tested routes for native search.
- The native UI's pre-approval “13 eligible”/MiniMax leading-option wording can overstate execution readiness. Approval filters it safely, but presentation should eventually distinguish benchmark eligibility from native qualification.
- Ordinary free-provider tool support, structured-output compliance and account entitlements remain unknown where not exposed/probed. No universal impossibility claim is made for untested providers.

There were **no production source changes**, no qualification-registry changes, and no source/backend installation. The audit is separable from #149. Its local evidence branch is `codex/mac-free-execution-audit`, based on #149 commit `264dcd07b`, in `/Users/Jo/GitHub/_worktrees/omnigent/mac-free-execution-audit`. Only diagnostic artifacts were added. No commit or draft PR was created because no production fix was justified by a newly qualified free route. The original `mac-next` worktree and its dirty accounting changes were preserved.

## Reproduce

Open the installed Omnigent app and select **Free execution audit — text**, **— coding**, and **— discovery**. Inspect their completed results, context meters and discovery call. The failed **— demanding** session records the high-floor runtime limitation.

From `/Users/Jo/GitHub/_worktrees/omnigent/mac-free-execution-audit`:

```sh
PYTHONPATH="$PWD" /Users/Jo/GitHub/omnigent-o3-routing-review-mvp/.venv/bin/python -m pytest \
  tests/server/test_o3_routing_review.py \
  tests/server/test_o3_tool_search.py \
  tests/server/test_o3_catalogue_recommendation.py \
  -q -p no:rerunfailures -o cache_dir=/private/tmp/o3-free-pool/pytest-cache
```

The private probe/reproduction scripts and live API snapshots remain in `/private/tmp/o3-free-pool`. Do not install their probe results as qualifications: **none passed native search**. To expand the pool, first qualify an exact currently free destination through the complete native protocol and only then resolve its remaining catalogue and descriptor requirements.

**CAPABILITY LIMIT — broader routes were enumerated, but none of the tested additional free routes qualified for safe native O3 execution.**
