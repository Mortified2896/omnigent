# Installed Omnigent Mac: startup-context investigation

The large startup context is real. The dominant source is eager tool-schema injection caused by the O3 routing alias lacking usable Codex search-capability metadata. In fresh native sessions, adding a session-private alias descriptor brought input down from **89,061 to 14,483 tokens**, without changing the route, greeting, native system-instruction text, or registered MCP capabilities. A paired catalogue control differing in only `supports_search_tool` reproduced **86,297 versus 14,483 tokens**.

This is a diagnosis plus a proven session-local workaround, not a production rollout. The accounting fixes remain intact. See [machine-readable evidence](evidence.json) for usage records, IDs, hashes, model provenance, controls, and checks.

## Fresh native measurements

All runs used the installed Omnigent service at `127.0.0.1:6768`, its existing Mac host, native Codex CLI **0.153.4**, and the same O3 alias `custom/o3-route-cf4f3181b533`. No separate Omnigent server or HomeLab service was started. Every first message was `hi`.

| First-turn field | Empty folder, normal inherited setup | O3 project/worktree |
|---|---:|---:|
| `last_token_usage.input_tokens` | 89,061 | 92,293 |
| `last_token_usage.cached_input_tokens` | 3,328 | 82,176 |
| Uncached input: input minus cached | 85,733 | 10,117 |
| `last_token_usage.cache_write_input_tokens` | 0 | 0 |
| `last_token_usage.output_tokens` | 11 | 14 |
| `last_token_usage.reasoning_output_tokens` | 0 | 0 |
| `last_token_usage.total_tokens` | 89,072 | 92,307 |
| `model_context_window` | 258,400 | 258,400 |
| Native app context display | 34% | 36% |
| Successful route-log model | `gpt-5.6-luna` | `gpt-5.6-luna-low` |
| Codex model alias | `custom/o3-route-cf4f3181b533` | same |

For both first turns, **every field of `total_token_usage` equals `last_token_usage`**. Thus these are first-request values, not cumulative values from an earlier conversation. The app's `last_total_tokens` agrees with `last_token_usage.input_tokens`, and its `context_window` agrees with `model_context_window`.

The app uses latest request input as its occupancy reading. The generated output and request total are reported separately; this investigation does not substitute either cumulative usage or uncached usage for that reading. Codex does not expose a separate exact post-response resident-context count here. Cached input still consumes context-window capacity; “uncached input” is not “new user text.”

The minimal folder was `/private/tmp/o3-context-investigation/minimal`, verified empty in the installed app. It still inherited normal global Codex capabilities. It was not a factory-reset Codex profile. The project was `/Users/Jo/GitHub/_worktrees/omnigent/mac-next`.

## Proof of freshness and installed-build use

| Case | Omnigent session | Native Codex thread |
|---|---|---|
| Empty folder | `46346de47d744632a39baeacacaaf0fc` | `01a0841a-def2-78a2-a3c9-eaca9474bc63` |
| Project | `3cc39b2c2bed48758e052f9235cc0579` | `01a0841d-106a-7bb3-bfda-1c3868d05445` |
| Alias descriptor, code mode | `5560120006d8420eae82451cd1fbc70b` | `01a0842e-199e-7532-a394-d314e0b3a810` |
| Alias descriptor, search on | `36f128317a4140ac8dbba8e573fbca71` | `01a0842f-fdf9-7341-a3f6-a15e16423238` |
| Alias descriptor, search off | `f2e4cddf1a114f3caf1f66bb70249593` | `01a08432-273a-78f2-856d-4e43ff42991b` |

Each create returned zero items and no existing external thread. Each run has its own private Codex home and rollout, null fork provenance, its own native thread ID and matching `prompt_cache_key`, and no `previous_response_id`. First-request inputs have no preceding assistant messages. The later context is not borrowed from the earlier “Friendly Greeting” sessions.

The installed `/Applications/Omnigent.app/Contents/Resources/app.asar` was dated September 9 at 09:08 HKT. The live backend listener was PID 64456 using the stable integration checkout's Python environment. New native runners were launched after the accounting source update and emitted the corrected values end-to-end. Native UI inspection verified both fresh greetings, their correct working folders, and 34%/36% meters. Source-only validation was not used as the acceptance proof.

The stable checkout and feature-worktree accounting module had the same SHA-256:
`67e4b6729dbba149d3e7832e758fc58900af959634c2a6eac7ff7183c16b5ff3`.

## Quantitative attribution

The retained historical first `Hi` request had **87,957 input / 3,328 cached / 11 output** tokens. The new empty-folder result reproduces the same order of overhead after the fix.

A replay of the captured new empty-folder payload produced **89,061 input tokens exactly** and returned authoritative `usage.attribution`. Its additive breakdown is:

| Component | Input tokens |
|---|---:|
| Tool field, including native tools and MCP/app schemas | **78,055** |
| Native system instructions | 4,369 |
| Skill catalogue/instructions | 4,137 |
| Permission instructions | 929 |
| Recommended-plugin list | 1,245 |
| Global AGENTS instructions | 81 |
| Environment description | 238 |
| Greeting and response-boundary input framing | 7 |
| **Total** | **89,061** |

Tools account for **87.64%**. There is no large unexplained remainder in this replay. It uses the provider's attribution, not a characters-per-token guess. Raw JSON tokenization was used initially for structural inspection, but it overcounts the provider's tool rendering and is not used as an additive substitute for this table.

Payload-only controls retained instructions and input-message hashes, used fresh diagnostic IDs, and removed one tool category. These are API request controls, separate from the native-session measurements:

| Control | Input | Cached | Successful response model |
|---|---:|---:|---|
| Full captured payload | 89,061 | 3,328 | `gpt-5.6-terra` |
| Connected-app namespaces removed | 38,172 | 3,328 | `gpt-5.6-terra` |
| All MCP namespaces removed | 18,796 | 3,712 | `gpt-5.6-sol` |
| Only Omnigent MCP removed | 82,182 | 4,480 | `gpt-5.3-codex-spark` |

The same-model connected-app difference is **50,889 tokens**. The other observed category differences are **19,376 additional tokens for non-app MCPs** and **6,879 for Omnigent's namespace alone**. These latter comparisons changed the route's resolved model, so treat their marginal attribution as route-level measurements rather than strictly fixed-model experiments. All returned non-tool field counts remained the same.

The normal outgoing request contained **477 callable tool entries with no duplicate qualified names**. Large contributors included GitHub, Google Drive, Todoist, OmniRoute, Anki, Gmail, Sites, and Omnigent. This was schema content, not fetched mailbox, document, repository-file, or adviser-conversation content.

The project delta was **3,232 tokens**. The tools and native system instructions had identical hashes in both fresh native requests. Local tokenization of the changed text accounts for the delta: additional repository skill descriptions +1,242, project AGENTS +1,955, permission text +5, and environment text +30. Those per-text numbers are tokenizer estimates; the aggregate 3,232 is observed native telemetry. No automatic source-file contents or git diffs were attached to the first request.

## Root cause: alias metadata controls eager versus deferred tools

The active custom model catalogue contains the real Codex model entries, including `supports_search_tool: true`, but contains **no entry for the dynamically generated O3 alias**. The unknown alias receives fallback native behavior. The resulting request eagerly contains the large MCP/app schema set.

The diagnostic catalogue lives only in temporary storage. Three newly created native sessions had private config copies pointing at it. A descriptor was cloned from the bundled/catalogued Luna entry, assigned the same O3 alias, and explicitly given the **observed fallback system-instruction text** so it was not replaced by a different model's prose.

| Native configuration control | First input | Cached | Output | Window | Route-log model |
|---|---:|---:|---:|---:|---|
| Normal unresolved alias | 89,061 | 3,328 | 11 | 258,400 | `gpt-5.6-luna` |
| Registered alias, search on, code mode | 15,237 | 0 | 13 | 258,400 | `gpt-5.6-terra-low` |
| Registered alias, search on, no code mode override | **14,483** | 0 | 14 | 258,400 | `gpt-5.6-terra-low` |
| Identical descriptor, only search support off | **86,297** | 0 | 11 | 258,400 | `gpt-5.6-sol` |

The last two catalogue files differ **only in `supports_search_tool`**. The native request generated locally changes from a compact client-executed `tool_search` surface to eager namespaces. The **71,814-token observed difference** follows that flag. Routing chose different actual models, but the large difference in request construction already exists before provider selection. Code-only tool mode is not required for the reduction. The 2,764-token difference between the unresolved alias and the explicit search-off descriptor reflects other descriptor-dependent rendering, so the entire 74,578-token normal-to-search-on difference is not attributed to that one flag alone.

A documented read-only `mcpServerStatus/list` call found **identical sets of 539 registered MCP tools** in normal and search-enabled native sessions. These include 37 Omnigent and 110 OmniRoute tools. The larger registry count than outgoing callable count reflects registered versus currently exposed tools, not duplicate injection. The registry was not disabled to obtain the low-context result.

The decisive local problem is loss of useful capability metadata at the routing-alias boundary, which switches Codex into eager schema delivery. It is not just a large alias string or adviser transcript copied into the prompt.

## Cache and leakage findings

- **Adviser-conversation leakage: NO in the inspected historical and fresh first-turn requests.** Their complete retained input lists contain bootstrap text plus the greeting, with no adviser messages or previous user/assistant exchanges. Hashes and role/length summaries are retained instead of republishing private transcripts.
- **Routing alias/fallback metadata materially contributes: YES, indirectly.** It controls schema exposure. The alias itself is small metadata; its missing capability description causes tens of thousands of schema tokens to be included.
- Both fresh normal requests had identical tools and system-instruction hashes. The project's 82,176 cached tokens are consistent with reuse of this large shared prefix. The exact cached byte ranges are not exposed. The new thread still had its own IDs and no prior messages.
- On the full-payload replay, all 3,328 cached tokens were attributed to the tool field. Cache use does not subtract those tokens from context occupancy.
- No evidence of fallback aliases expanding into a large extra textual prompt, automatic adviser transcript attachment, duplicated tool names, or conversation restoration was found.

## Changes, checks, and limits

No production code, global Codex config, normal model catalogue, accounting behavior, route/combo definition, or installed package was changed. Five diagnostic conversations, temporary private configurations, payload controls, and this report/evidence were created. Diagnostic rollouts/configurations remain available; no logs or credentials were deleted. No commit or push was made.

The temporary alias entries are a proof of mechanism, not a general production fix: they borrow capability metadata from one Codex model while the alias can choose several models/providers. Permanently advertising that metadata without validating client tool-search transport and tool use across eligible routes could create a different routing bug. The production correction needs capability-aware alias metadata or equivalent routing/client separation; blindly cloning one model into every mixed-provider alias is not a safe final patch.

Checks performed:

- Five fresh native first-turn runs completed; all first `last` and cumulative usage records reconcile, IDs match, and effective windows remain 258,400.
- Four schema-attribution replays completed. The full replay reconciles exactly to 89,061 tokens and exposes authoritative component accounting.
- One-flag catalogue comparison and identical MCP registry sets were asserted programmatically; assertions passed.
- Focused existing native accounting tests: **18 passed, 1 failed, 207 deselected**. The failure is `test_forwarder_posts_codex_usage_live_per_frame`, whose total-only fixture still expects `context_tokens` to be manufactured from cumulative input. That pre-existing expectation conflicts with the preserved accounting fix. No test or accounting source was edited to hide the failure.
- Pytest's first invocation was blocked by the `pytest-rerunfailures` plugin binding a localhost socket. The test invocation with `-p no:rerunfailures` produced the result above.
- Initial and final pre-existing code diff: two files, 60 insertions / 23 deletions; HEAD `585ca6d16231bda45c02a3d2b9d3407b1309b5a4`. This investigation adds only diagnostic artifacts to the worktree.
- Project native UI showed a Bitwarden startup warning, but the greeting completed and both outgoing normal tool sets were identical; this did not explain the context delta.

Automatic approval review initially blocked test execution; the user explicitly approved the diagnostic runs, which then proceeded. Review later blocked an additional model-driven tool-discovery prompt. The safer read-only registry comparison succeeded. Actual tool-search-and-tool-execution behavior across all routed providers remains unverified, which is why the temporary descriptor was not installed as a global fix.

This investigation does not establish a factory-clean upstream Codex baseline or a fixed-model cross-provider cache benchmark. Neither is required to explain the reproduced excess: the same installed native runtime, with the same registered tools and a controlled local metadata change, drops into the 14–15k range.

## How to verify

In the installed Omnigent app, open **Context diagnostic — minimal** and **Context diagnostic — project**. Each has a single `hi` exchange; the displayed percentages should be 34% and 36%. Native session URLs are:

- `http://127.0.0.1:6768/c/46346de47d744632a39baeacacaaf0fc`
- `http://127.0.0.1:6768/c/3cc39b2c2bed48758e052f9235cc0579`
- Search-enabled diagnostic: `http://127.0.0.1:6768/c/36f128317a4140ac8dbba8e573fbca71` — 14,483 input, approximately 6%.

Inspect `evidence.json` for each case's original rollout path and complete first-turn token fields. The private diagnostic request bodies and scripts remain in `/private/tmp/o3-context-investigation`; they were not copied into this shareable evidence file.

To reproduce the existing accounting test result from this worktree:

```sh
PYTHONPATH=/Users/Jo/GitHub/_worktrees/omnigent/mac-next \
  /Users/Jo/GitHub/omnigent-o3-routing-review-mvp/.venv/bin/python -m pytest \
  tests/test_codex_native.py -q \
  -k 'session_usage_data or usage_coalescer or posts_codex_usage' \
  -p no:rerunfailures \
  -o cache_dir=/private/tmp/o3-context-investigation/pytest-cache
```

**LOCAL BUG — missing routing-alias capability metadata causes unnecessarily eager startup schema injection.**
