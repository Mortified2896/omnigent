# O3 dependency audit during the v0.16 integration

Audit performed on 2026-10-01 after `git diff --name-only --diff-filter=U`
returned no files. The integration has prepared parent
`647f4d28502b8c393d6cfca18ec293a53f344c5e` and upstream merge parent
`82a74473ee4c01163c4269f4296b10a158f0e47e`; it is not yet a committed or
accepted deployment. This audit is independent of the conflict inventory.

## Scope and method

Search the whole tracked tree with `git grep -n -i -E
'\bo3\b|o3_|_o3|tool.free|benchmark.capture'`, excluding binary images and
lockfiles. Inspect imports, callers, environment gates, registration and
persistent readers, including modules whose filenames do not contain O3.
A mention of a standard model such as `o3`, or the package `boto3`, is not
itself an experimental dependency. Private diagnostic output is outside the
repository; no credentials or stored payloads are part of this audit.

## Findings and disposition

| Area | Actual dependencies and configuration | Disposition |
| --- | --- | --- |
| Landing composer | Upstream `NewChatDialog.tsx` is the structural base; retained additions are Model Advisor, qualified model/access-lane/effort selections and fresh bypass consent. | O3 proposal, estimator, draft restoration and failed-review UI are omitted from this composer. |
| Desktop profile | `web/electron/src/o3_local_profile.js` rewrites loopback endpoints and enables O3 and hard tool-free mode; its remaining direct importer is its unit test. | The upstream-based desktop entrypoint no longer imports or invokes it, canonicalizes endpoints to O3, or automatically enrolls a Mac host; retire the dedicated app profile and its profile-only test following the owner’s clarification that the O3 app is no longer needed; routing remains preserved separately. |
| Desktop recovery | `local_server_recovery.js` is independent of O3; it serializes local-server recovery and refuses a different returned endpoint. | Retain renderer crash/hang, focus, launch and watchdog recovery with upstream auth/navigation behavior, deep-link preservation and watchdog disposal. |
| Current OmniRoute transport | `codex_native/app_server.py` imported `_codex_mcp_omniroute_key` from the O3 adapter for existing configured transports. | Extract the identical reader to `omnigent/omniroute_credentials.py`; current transport imports the neutral module, while O3 retains a compatibility alias exercised by its tests. Existing environment aliases and transport policies remain unchanged. |
| Server routing experiment | `server/app.py` imports the O3 service/error type; router registration is gated by `OMNIGENT_O3_ROUTING_REVIEW`. `host/connect.py` has a gated reader; local-server configuration fingerprints include O3 settings. | Preserve disabled until server registration, configuration compatibility and historical API readers can be retired together. |
| Historical session policy | Session create, update, message, fork/retry and cleanup routes call O3 approval and recorded-policy helpers; `chatStore` and `ChatPage` also protect historical benchmark labels. | Retain guards that prevent historical approval/routing identity from being rewritten or reused; these are data protections, not evidence that experiment execution should be enabled. |
| Historical review UI | `components/chat/Transcript.tsx` imports `O3SessionReview`; its query is gated by the server capability. `ProposalStore` reads private `o3-routing-review/state.json`; session labels bind proposals to historical responses. | Preserve the disabled historical reader and store; do not delete proposal state or relabel existing sessions. |
| Disconnected proposal UI | `RoutingProposalCard` depends on `ExecutionProfile` and `O3DecisionInspect`; timing components and routing-review client helpers retain tests and dependencies within this group. | Retirement candidates with no restored landing entrypoint; keep disconnected rather than infer that all callers or persisted browser drafts are disposable. |
| Tool-free experiment | `harness_plugins.py` registers `local-tool-free`; the executor/harness import proposal, transport and protocol helpers, validate approved proposals, enforce prompt fingerprints and use private at-most-once claim files. Server agent installation is gated by `OMNIGENT_O3_HARD_TOOL_FREE`. | Preserve disabled while registrations, stored agents/bundles, proposals and claim readers are reviewed together; current Model Advisor uses the separate `host/advisor_call.py` implementation. |
| Benchmark capture | `inner/codex_executor.py`, native app-server startup/close, forwarder and policy hook call capture helpers; native begin/finish/close and `capture_io` are gated by `OMNIGENT_BENCHMARK_CAPTURE=1`. Export and rollout helpers read saved captures and rollouts. | Preserve disabled and retain exporters/readers; do not enable a capture pipeline or delete historical capture/rollout assets. |
| Model identifiers | `models/model_override.py` accepts historical `custom/o3-route-*` identifiers and native startup has an alias-catalog path for those identifiers. | Preserve compatibility until historical sessions and reconnect/resume consumers are assessed; ordinary catalog model names are not O3 experiments. |
| Deployment compatibility | `peer_deployer.rtx` enforces O3 disabled; `external_rtx` is the independent-controller promotion path. `peer_deployer_lib.sh` and the two old shell promotion entrypoints have safety/refusal regression callers. | Retain current safety helpers and inert refusal shims; do not revive their old deployment paths or weaken the disabled-state check. |

## Live disabled-state evidence

Read-only inspection of `/proc/<MainPID>/environ` for the existing
`omnigent-o1.service` and `omnigent-o2.service` found all three experiment
switches disabled or unset: `OMNIGENT_O3_ROUTING_REVIEW`,
`OMNIGENT_O3_HARD_TOOL_FREE`, and `OMNIGENT_BENCHMARK_CAPTURE`.
No live state, credentials, permissions or services were changed.

## Validation boundary

The focused composer/new-chat/indicator suites passed 999 tests; Electron
navigation/recovery suites passed 76 tests; O3/tool-free/benchmark capture
unit suites passed 103 tests before the credential helper extraction.
After extraction, the original helper parity, current native transport and
test-session policy suites passed 153 tests. Full upgrade acceptance, snapshot regeneration,
restoration rehearsal and O1/O2 deployment remain separate required gates.
All eight conflict PNGs currently use temporary upstream baselines and must
be regenerated and reviewed after the integrated UI passes validation.
