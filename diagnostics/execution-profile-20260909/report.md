# Local Mac O3 execution requirements — 9 September 2026

Estimator requirements now populate the review controls directly. Changing a field creates a persisted user override; reset or setting the estimator's value removes it. Review and approval use the effective requirements. Native tools remain available and the independent tool-search qualification gate is unchanged.

## Capability inventory and UI

The backend contains `terminal`, `tools`, `minimum_context_tokens`, `vision`, `input_modalities`, `output_modalities`, `minimum_input_tokens`, `minimum_output_tokens`, `structured_output`, and the fixed `responses` client endpoint.

The UI exposes **Tools required, Image input, Image generation, Structured output, and Minimum context (tokens)**. Each starts from the estimator's effective value, without an Auto selector or untouched-field badges. Changes revalidate; they do not execute. Only non-null overrides show “Overridden” and an individual reset button. Numeric changes use Apply, so partially typed numbers are never submitted. Decided proposals have disabled controls.

The inspected forecast catalogue has 3,152 rows. Input modality evidence is text+image in 1,951 rows, text-only in 15, and unknown in 1,186. Output evidence is image in 192, text in 1,770, video in 4, and unknown in 1,186. These are metadata counts, not executable-route or availability claims. See [catalogue-capabilities.json](catalogue-capabilities.json).

Deliberately omitted controls:

- Audio input/output and video input: no affirmative modality metadata in this snapshot.
- Native web/search: no unambiguous first-class capability; MCP tool discovery is not internet search.
- Terminal/shell: a harness capability, without a separate catalogue model gate. The existing legacy candidate gate remains.
- Separate minimum input capacity: the current catalogue gate shares context capacity; it is not independently qualified input capacity.
- Minimum output capacity: API/model support remains, but all catalogue limits are unknown, so it is not exposed as a numeric UI control.
- Hard tools-disabled execution: no proven native enforcement hook exists. It remains a separate follow-up.

Structured output has a genuine existing eligibility gate, but all inspected metadata is unknown. Requiring it therefore gives zero eligible routes; this control does not assert provider support. Image-only output metadata also does not establish Responses execution compatibility, benchmark adequacy, or simultaneous text output.

## Data model, API, and routing

```text
proposal.adviser.requirements                 (preserved estimator evidence)
       + proposal.requirement_overrides      (nullable per-field user choices)
       = proposal.effective_requirements     (computed response field)
```

`RequirementOverrides` validates partial patches. Omitted fields keep their current override; a null resets that field. A value equal to the current estimator value clears a redundant override. Untouched/null fields resolve to the estimator. The atomic proposal store persists adviser requirements and overrides; effective requirements are recomputed, not independently stored. Additive fields preserve existing schema-v1/v2 loading. `constraint_version` increments on adjustment, while prompt fingerprint and proposal identity remain unchanged. New proposals start with no overrides. Existing quality adjustments preserve capability overrides and do not replace estimator requirements.

The existing `PATCH /v1/o3/routing-review/proposals/{id}` accepts, for example:

```json
{"requirement_overrides":{"tools":false}}
```

Reset just that field:

```json
{"requirement_overrides":{"tools":null}}
```

API override fields are `tools`, `image_input`, `image_output`, `structured_output`, `minimum_context_tokens`, and `minimum_output_tokens`. Invalid types, negative capacities, and unknown fields are rejected. `image_input` resolves both `vision` and image membership in input modalities, avoiding contradictory overrides. It preserves other modalities. `image_output` changes only output image membership and preserves other output requirements.

Catalogue reranking uses effective requirements at adjustment and again at approval. Legacy evaluation now fails closed for required modalities, structured output, and output limits that its older candidate metadata cannot establish. Resource snapshots/advice refresh with the new candidate set. Capability-only adjustments preserve an existing manual benchmark floor; they no longer silently recalibrate it. Adviser instructions now explicitly distinguish self-contained text tasks from tasks requiring external tools.

**Tools OFF means not required, never forbidden.** Tests admit both tool-capable and non-tool-capable candidates when OFF. Native approval still checks exact tool-search qualification for every destination, including after an explicit OFF override. No qualification, fallback, tool registry, or native launch configuration was weakened.

Image input, model-generated output, and images returned by tools remain distinct. Tool-result images never satisfy model image-generation evidence. The output toggle adds image to the estimator's other output requirements; it does not silently remove an existing text requirement.

## Installed Mac acceptance

The actual `/Applications/Omnigent.app` was built and installed. Its Electron `app.asar` SHA-256 is `28b0c8ef97f195864becf475a103e36722d2e906e49cd3a71e2f211e35b2352d`. The web production bundle and five changed backend modules were installed into the stable Mac checkout. Its Python environment remains the installed app's environment; the separately built wheel was not substituted for that editable installation. [install-manifest.json](install-manifest.json) records before/after module hashes.

The stable checkout's existing #149 changes were verified and preserved. The previous app, backend files, and web bundle are retained at `/Users/Jo/.omnigent/o3/execution-profile-backup-20260909`. Native sessions were idle before backend updates. Only one local backend ran on port 6768. No other native server, O1/O2 runtime, VPN, credential, or permissions configuration was used or changed.

The installed Electron window was driven through Playwright after native accessibility automation lost its page on reload. The screenshots below are from that installed app, not a separate browser preview.

| Scenario | Estimator / override | Live result |
|---|---|---|
| Fresh coding review | Tools ON, no overrides | 13 capability-eligible routes; native approval retained 12 qualified routes |
| Tools override | ON → user OFF | 13 routes; persisted across app reload; original remained ON |
| Reset | Remove tools override | ON restored; no override badge; same original evidence |
| Context | 0 → user 1,000,000 | 13 → 1 capability-eligible route; the remaining MiniMax route is not native-qualified and was not executed |
| Image input | OFF → user ON | 13 remained eligible; 15 text-only and 1,219 unknown-input routes were excluded across the full evaluated set |
| Fresh prompt-only review | Estimator Tools OFF and terminal OFF | 13 capability-eligible → 12 native-qualified; completed uppercase response |
| Structured output, local-rule proposal | OFF → ON | 13 → 0; metadata remained unknown |
| Image output, local-rule proposal | OFF → ON | 13 → 0; no invented output compatibility; reset restored 13 |

The coding session read the fixture with `exec_command` and explained the return value. The final text session completed without tool calls. See [routing-decisions.json](routing-decisions.json) for estimator, override, effective requirements, versions, eligible IDs, and exclusion counts.

The first text experiment still estimated Tools ON. After clarifying adviser instructions, the fresh model-backed review correctly estimated OFF; the controls displayed each actual result rather than forcing an expected value. Two attempts at an additional image-estimator proposal returned recoverable `omniroute_unavailable` 503 errors. Image-requirement acceptance is therefore the successful live override scenario above, not a claimed successful image-estimator run or image execution. No image task was executed.

## Lazy discovery and current context

| Native scenario | First input tokens | Registered MCP tools | Result |
|---|---:|---:|---|
| Coding | 16,458 | 539 | File read and explanation completed |
| Prompt-only, Tools OFF | 16,447 | 539 | Uppercase response completed |

Both registry hashes equal the prior audit's complete registry hash. All normal MCP tools remain registered, while ordinary startup remains in the 16k class rather than returning to approximately 89k eager schemas. Native/fallback qualification regression tests pass.

For coding, the final current context is **18,085**, exactly the latest request input, while cumulative input is **34,543**. Cached input remains separate. The effective native context window stayed 258,400. [native-telemetry.json](native-telemetry.json) contains first/last usage and registry evidence.

This feature does not create a hard tool-free native lane. Tools OFF retains the full registry and independent native qualification gate. No additional free route is claimed qualified, and the pre-approval candidate count is not a guarantee of native readiness.

## Tests and visual proof

- **89 passed**: routing review, catalogue eligibility, and native tool-search tests. Coverage includes partial override, original preservation, deterministic effective values, reload/store fetch, reset, redundant override removal, new-proposal isolation, quality/floor preservation, versions, modality/capacity/unknown metadata, and native qualification with Tools OFF.
- **204 passed**: proposal component, API client, session-event/context accounting, and new-chat flow tests.
- **Browser end-to-end driver passed**: change tools, reload, reset, adjust quality, approve/launch exactly once. It uses the existing fake-backed browser test against the installed server; native resource endpoints are mocked to avoid dependence on a retired fixture terminal. No competing native server was started.
- TypeScript, Ruff, Pyrefly, all applicable scoped pre-commit hooks, production web build, wheel build, and macOS Electron build passed. Existing CSS highlight/chunk-size and wheel license warnings remain.

Screenshots:

- [A — estimator-populated coding profile](execution-A-initial.png)
- [B — Tools OFF marked overridden](execution-B-overridden.png)
- [C — reset restores estimator Tools ON](execution-C-reset.png)
- [D — context override reduces the candidate set](execution-D-context.png)
- [E — independent image-input requirement](execution-E-image.png)
- [F — native coding result](execution-F-coding-result.png)
- [G — fresh estimator Tools OFF](execution-G-text.png)
- [H — native text result](execution-H-text-result.png)

## Delivery and verification

The branch is `codex/mac-execution-profile-overrides`, stacked on #149's verified head `264dcd07b2eb7c4cefbe96d07e733cde59c6aa30`. Its draft PR targets `codex/mac-lazy-tool-discovery`, not `main`. Tracking issue: #150. Nothing was merged, released, or deployed to O1/O2.

To verify in the installed app, create a fresh O3 review. Change Tools required, reload, and confirm the override remains. Use its reset arrow and confirm the original value returns. Apply a 1,000,000-token context minimum and compare the candidate count; reset it before normal approval. Availability and catalogue counts can change. Inspect **Explain greet.ts Return Value** and the latest **Uppercase Hello World** sessions for completed native results.

From this worktree:

```sh
.venv/bin/pytest tests/server/test_o3_routing_review.py tests/server/test_o3_catalogue_recommendation.py tests/server/test_o3_tool_search.py -q -p no:rerunfailures
cd web
pnpm exec vitest run src/components/RoutingProposalCard.test.tsx src/lib/o3RoutingReview.test.ts src/lib/sessionEvents.test.ts src/shell/NewChatDialog.flow.test.tsx
pnpm run type-check
```

Remaining follow-ups are true tools-disabled native enforcement, richer qualified modality/output/structured metadata, clearer pre-approval versus native-ready candidate presentation, and retrying image-estimator acceptance when its provider path recovers. Existing UI can still show MiniMax as a pre-approval leader; approval safely removes it when unqualified. No capability or provider success was manufactured to close these gaps.
