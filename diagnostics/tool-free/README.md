# Local O3 hard tool-free execution acceptance

This is a draft stacked change on `codex/mac-tool-free-execution`, based on
`origin/codex/mac-execution-profile-overrides` at
`6d4f1ef698c9e55c6a2645f7abd378565f9329d7`. The worktree is
`/Users/Jo/GitHub/_worktrees/omnigent/mac-tool-free-execution`.
Implementation, builds, tests, and Git writes were confined to this worktree.
O1/O2 were not used or changed. No merge or release is authorized by this report.

## Execution contract

The implementation uses a dedicated local Responses executor. Native Codex's
existing tool suppression was not sufficient proof of zero callable tools.
The separate `local-tool-free` agent has an empty tool map and no spawn capability.
Its executor declares `supports_tool_calling=False`, discards any supplied tool
callbacks/specifications, and never emits a tool request or dispatches provider
output. An unexpected tool/function/MCP/computer-call output is a protocol error.

The closed request builder sends an exact provider/model route, `tools: []`,
`tool_choice: "none"`, `store: false`, and `stream: false`; it does not accept
arbitrary caller parameters, Combo routes, previous-response IDs, tool discovery,
or continuation state. The runtime checks the approved prompt fingerprint,
constraint version, selected route, expiry, and effective requirements. An atomic
claim consumes the approval once, including across process restarts. The current
MVP accepts one text turn; every follow-up requires a new routing review. There is
no automatic promotion of a running tool-free task to a tool-capable session.

The proposal explicitly stores `tool_capable_native` or `hard_tool_free` execution
options, the selected provider/model, cost class, conservative quality bound,
selection reason, exclusions, and execution provenance. Provenance includes the
actual request contract, reported provider/model/cost, response ID, and usage.

## Routing and requirements

Tools OFF continues to mean *not required*. Both execution modes remain eligible;
Tools ON removes the tool-free options. Price ranks only candidates that meet the
unchanged floor. `preserve_subscription` and `lowest_cost` prefer a qualified free
route. The existing native approval/Combo and ResourceAdvice paths remain the
fallback when no suitable free route survives qualification and approval recheck.

Free qualification uses the current model catalogue and exact current pricing.
An explicit free suffix or exact zero input/output prices permits a bounded probe;
unknown prices and non-chat prices do not. Successful execution additionally
requires exact requested provider/model attribution and zero reported cost.
Catalogue aliases are preserved: an enumerated `oc/...` route requests provider
`oc`; its canonical catalogue group is recorded separately. An unproven different
provider, such as `opencode-zen` for a request to `opencode/...`, remains excluded.

Qualification probes at most 40 candidates, with concurrency six and a 30-second
per-probe deadline. Results are reused for five minutes for the same floor and
reasoning effort. Approval still performs an independent recheck (60-second
maximum per candidate), evaluating remaining free/native candidates on failure.
Qualification and approval use the same 1,024-token output budget as execution.
The initial 128-token probes could exhaust their budget on reasoning and return
no text. The 12-second initial deadline was also too short: a real free response
took 25.7 seconds.

The initial executor supports text input/output with a 1,024-token output cap.
Image input, image output, and structured output are distinct requirements and
are conservatively rejected for this executor. Context and output minima are
checked against known provider and executor limits. This is a limitation of this
executor, not a definition that all tool-free models are text-only. Estimator
requirements are retained separately from persistent/resettable user overrides.

## Live inventory and evidence

The reproducible audit script is `probe.py`; run it with the normal local O3
catalogue and OmniRoute environment. No credentials are written into these
artifacts. `catalogue-inventory.tsv` records the full observed route inventory;
`audit-matrix.json` contains detailed free-candidate observations and exclusions.

The full audit used a 15-second deadline and 128-token output budget; current
production qualification uses 30 seconds and 1,024 tokens. These observations
therefore describe that earlier audit, not exhaustive current availability.

At 2026-09-10 04:44:24 UTC:

- 3,184 exposed IDs; 3,096 non-Combo catalogue routes.
- 34 explicit free-labelled routes plus eight exact zero-priced chat routes.
- 42 probes: three protocol responses, 39 failures/timeouts.
- Two exact-attribution, zero-cost observations: `oc/mimo-v2.5-free` and
  `oc/nemotron-3-ultra-free`. Both have conservative quality 20, so neither passes
  floors 55 or 85. They no longer require native Codex tool-search qualification.
- `opencode/nemotron-3-ultra-free` responded but reported `opencode-zen`; excluded.
- Other failures include model unavailable, authentication/support errors,
  unsupported `tool_choice: none`, empty text, cooldowns, and bounded timeouts.
- Earlier same-day probes returned more OpenRouter successes; availability is
  time-sensitive. A later direct Liquid request returned a provider cooldown.

Provider-wide labels alone do not establish zero per-route pricing. Routes with
unknown current cost were inventoried but not probed as free. Account requirements
and unsupported/unknown capabilities remain explicitly unknown where the catalogue
does not expose them. No former MiniMax subscription is assumed.

`outbound-requests.json` records real HTTP request-hook observations from the audit.
`direct-execution.json` records a real executor response from `oc/mimo-v2.5-free`:
25.7 seconds, 248 input tokens, 103 output tokens, zero cost, cache MISS, and an
empty tools array with tool choice none. This is direct protocol proof, separately
identified from installed-app acceptance.

## Installed Mac app acceptance

Testing used `/Applications/Omnigent.app` at `127.0.0.1:6768` in a deliberate local
test window, backed by the feature worktree's Python server and host. The stable
integration checkout was not edited. Existing idle/failed session histories were
preserved. The app's auto-started stable host was replaced with the verified
feature host before testing.

- Normal estimator review of the uppercase task selected a free route at common
  floor 20. The approval-time free recheck timed out, so the normal native fallback
  ran and returned `HELLO WORLD`; this is not claimed as tool-free chat success.
- Tools ON selected the native mode. Reset restored the estimator's Tools OFF.
- An image-input override selected native; resetting restored the original value.
- Hard difficulty with Tools OFF retained the higher floor and selected native.
- Actual screenshots are included; they are from the installed app, not mocks.
- Fresh native fallback telemetry (`native-fallback-telemetry.json`) records
  19,233 first-input tokens, 3,328 cached input tokens, and seven output tokens.
  This is above the earlier ~16k baseline but does not return to the ~89k startup.
  It is a prompt-only fallback measurement, not a new repository-coding benchmark.

## Verification and reproduction

From the feature worktree:

```sh
TMPDIR="$PWD/.cache" OMNIGENT_DATA_DIR="$PWD/.cache/test-data" \
  .venv/bin/pytest tests/server/test_o3_tool_free.py \
  tests/server/test_o3_routing_review.py tests/server/test_o3_catalogue_recommendation.py \
  tests/server/test_o3_tool_search.py tests/runner/test_codex_native_launch_config.py -q
(cd web && pnpm exec vitest run src/components/RoutingProposalCard.test.tsx src/shell/NewChatDialog.flow.test.tsx)
```

To reproduce in an intentional feature runtime window: open the installed app,
enter a prompt-only greeting or short text transformation, choose Review route,
leave Tools required OFF and preserve-subscription preference, and inspect the
selected execution mode. Approve a qualifying free route. Verify returned text
and persisted `tool_free_provenance`. A second turn must fail with a new-review
message. Repeat with Tools ON, a higher floor, or image input and confirm the
native path or explicit ineligibility. Do not start competing native runtimes.

The final installed-app tool-free acceptance succeeded on 2026-09-10 at
06:13:05 UTC. Proposal `887f4017-34f1-4f29-ab50-ddf693b61f7e` launched session
`05895ba3cd1d4fa7922a8893643f65e0`, displayed the actual response from
`oc/mimo-v2.5-free`, and recorded exact attribution and zero cost. The actual
request contract had `tools: []`, `tool_choice: none`, and no continuation state.
Usage was 46 input tokens and 178 output tokens (145 reasoning), total 224.
The installed app then rejected a request to read local files with
"Tool-free follow-ups require a new routing review." The rejection occurs before
provider execution or tool dispatch. See `installed-execution.json`,
`tool-free-output.png`, and `tool-free-followup-rejected.png`.

Runtime integration fixes were validated through real bundle loading, the runner's
spawn-environment builder, and adapter text-event translation. The built-in YAML
uses an empty tool map; proposal binding is validated when an executor is created,
after session prewarming; text is emitted through the existing chat stream.
During the test window the installed shell's CLI path was temporarily pointed at
the feature worktree so its supervised host restarts used the matching runner.


## Final regression results

- 150 focused backend tests passed, including real bundle loading, runner model
  binding, zero-tools HTTP requests, adapter text delivery, replay protection,
  protocol violations, follow-up refusal, capability/floor filtering, lazy tool
  qualification, requirement overrides, and native launch configuration.
- 83 frontend flow/card tests passed; TypeScript, Pyrefly, lint, and production
  frontend build passed.
- Fresh native repository session `460d09548ec944ec845af75b24515e00` selected
  Tools required ON and qualified `codex/gpt-5.5`, read and fixed only the
  worktree-local acceptance fixture. First input: **15,078 tokens**, cached 3,328,
  context window 258,400. See `native-repository-telemetry.json`.
- Specialist MCP registration/discovery code is unchanged by this diff. The
  current native run did not enumerate all 539 globally registered tools; this
  report does not claim a new live count. The observed MCP startup also reported
  a Bitwarden startup failure, which is separate from the tool-free executor.

The native assertion initially used missing `python`; its continuation hit a
provider 503. A normal follow-up using `python3` exited zero and returned
`greet("Jo") == "Hello, Jo!" passed. This separates provider availability from
native tool execution and startup-context validation.

## Runtime restoration

After acceptance, the feature server and shell-owned host were stopped. The
installed shell's original stable CLI path was restored without changing other
settings. The stable server/host were restarted on port 6768. Feature source and
its draft stacked PR remain isolated; there was no merge, release, or O1/O2 work.
