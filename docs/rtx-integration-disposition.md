# RTX integration feature disposition

Status (2026-09-14): RTX O1 on VM 100 `rtx-omnigent` is the active runtime.
This document preserves integration provenance and acceptance checkpoints;
source publication and historical peer-promotion acceptance are distinct
from current runtime status. For present-state decisions, defer to the
[HomeLab current topology](https://github.com/Mortified2896/HomeLab/blob/codex/rtx-o1-migration/docs/omnigent-current-topology.md) and [operational workflow](https://github.com/Mortified2896/HomeLab/blob/codex/rtx-o1-migration/docs/codex-server-workflow.md).
Old O1/O2, OpenCode Web and old OmniRoute on `ai-control-hub` are retired.
Do not start, restore or recreate O2 to satisfy legacy deployer preflight.

Historical integration baseline: origin/main 28095733f87dc50b3bf4e73adee39ad4a7e72c1d.
The historical O1/O2 release ba50146e9bd4512ec345feb7e9bc483421a04104 is
an ancestor. Both old server/host pairs were active and healthy on 2026-09-13.
At that checkpoint, the Mac O3 process used the mac-tool-free-execution worktree at
06c14e783c168474321f43802d9aceaaa9889365 with preserved untracked diagnostics.
Mac OmniRoute reported 3.8.50 / build dea6bb8 / Node 22.22.3.

| Feature | Disposition | Source / integration boundary |
| --- | --- | --- |
| Native execution, durable sessions/approvals/results, publication and repository safeguards | ALREADY PRESENT | Deployed release ancestry retained through current main; focused native/transport regression gates remain required. |
| O3 estimator, versioned calibration and approved catalogue execution set | ALREADY PRESENT | PR 140 is merged; current main includes its corrections. No recalibration. |
| Qualified lazy discovery and alias metadata | SEMANTIC PORT | Exact missing commit 264dcd07b2eb7c4cefbe96d07e733cde59c6aa30 from PR 149. Linux gateway qualification must be fresh. |
| Capability/reasoning overrides, original recommendation and reset | SEMANTIC PORT | Exact missing commit 6d4f1ef698c9e55c6a2645f7abd378565f9329d7 from PR 151. |
| Separate hard tool-free lane and its limitations | SEMANTIC PORT | Exact missing commit 06c14e783c168474321f43802d9aceaaa9889365 from PR 152. |
| Mac telemetry installation, archive, rollback and historical counters | KEEP | PR 154 head 7f72743727160f9c132f786d792b7022dac94c7e remains preserved; do not run Mac adoption on Linux or migrate its archive. |
| Minimal Codex OTEL config and optional provenance hook ownership | SEMANTIC PORT | Only native runtime helpers and focused tests from PR 154. Optional capture must not block execution. |
| Visible Omnigent Smart Routing / Benchmark Routing (O3) / manual selection | INTEGRATED / TESTED | Implements the approved issue 113/124 controls using native selection and dispatch. Preserve unsupported reasons and isolate policies. |
| Linux runtime qualification, immutable artifact and historical cross-host plan | RTX ACCEPTANCE RECORDED / LEGACY PLAN SUPERSEDED | Linux 3.8.50, immutable artifact boot and real native/O3 tasks passed; single-route protocol checks do not qualify every fallback. The unimplemented legacy cross-host transaction is not required for the current single-primary RTX workflow. |
| Extra O2/O3 installations, GPU rework, new observability and benchmark platform | DISABLE-DEFER | Outside the initial single-guest migration. |

The three Mac feature commits form an ancestry-checked sequence. They are
applied individually to the current product baseline, without overlaying a
runtime checkout or merging their old parent branches. Mac-only telemetry
infrastructure is deliberately not installed on the new guest.

Exactly one selected routing policy owns each new turn. Native mode retains
native chooser semantics and never invokes O3. O3 approval must never be
followed by a native semantic chooser or an unrestricted fallback. Executor
effort is separate from estimator inference effort. Endpoint-bound evidence
is revalidated before an approved execution set can run.

Publication is the existing customization PR #156. Current RTX release changes
require accepted/approved source, immutable artifact identity, consistent
backups, external control, runtime verification and executable rollback per
the HomeLab workflow. The former requirement for real O2 supervision and
final transfer from the old peers is superseded. Historical databases remain
separate recovery evidence and must not replace active RTX history.

## Current integration validation

The shared model picker now offers explicit native, benchmark and manual
policies. New users remain on default/manual. Stored explicit selections are
preserved; unavailable selected routing blocks submission with its reason.
Native dispatch sends no O3 proposal or pinned executor effort. O3 approval
explicitly disables native routing. Session labels record requested policy
and native backend without asserting an unobserved provider result.
Switching an unsent context invalidates a pending review; stale asynchronous
responses cannot revive it. Creation freezes the controls.

O3 persists the original estimator analysis separately from adjustments.
Execution reasoning changes and reset revalidate without lowering the current
floor. Modified legacy reviews with missing originals cannot invent a reset.
The native/manual effort picker validates against current model options and
persists the selected supported value.

Validation on ai-control-hub: 240 focused integration/native tests passed,
51 O3 tests including new reset/legacy checks passed, 86 frontend tests
passed, TypeScript passed, and the production frontend build passed.
Two Playwright scenarios cover 1280x900 desktop and 390x844 mobile:
mode discovery, explicit O3 opt-in, capability override/reset/reload,
execution effort override/reset, waiting and exactly-once approved dispatch.
Rendered screenshots under diagnostics/rtx-ui are from this production UI
with mocked routing responses; they do not prove live provider or phone use.

Actual Linux gateway transport evidence belongs to the companion runtime
PR: both low and medium Responses probes completed with matching response
model/effort metadata. That does not qualify all historical catalogue entries,
the full native edit/test loop, tool search, or a hard tool-free endpoint.

Gear saves preserve the benchmark policy unless a routing/model option was
explicitly selected. Resumed conversations display their recorded policy
and native backend details; O3 conversations do not expose native routing
toggles. Native wording distinguishes model selection from the harness
reasoning default. The final focused frontend run passed 235 checks,
including these settings and resume regressions.

## Live Linux acceptance and saved O3 configuration

The isolated RTX service completed manual/native fixture read/edit/test,
restart/reconnect, an O3 task with correlated allowed failover, and a real
built-in OSS Smart Routing task selecting codex/gpt-5.6-sol-max. These are
separate tasks, not a comparison experiment. Installed evidence and real
desktop/mobile viewport screenshots are recorded in HomeLab's RTX runbook.

Live O3 continuation on artifact a7d9fa39ee261d3ad9adc3514214c624aa15af79
passed the four fixture tests. Model, effort, policy-relabel and fork requests
returned 409 and left the saved configuration unchanged. The UI audit found
that an unsupported effort edit could still appear in local picker state.
The controls now show the approved configuration as read-only with a new-review
explanation, and resume hydrates O3 model/effort without unrelated sticky picks.
462 focused composer/store tests, TypeScript and scoped pre-commit passed.

The single-route qualification CLI runs one explicit route with a 45-second
overall deadline and never activates a registry. RTX codex/gpt-5.5 passed
tool-search continuation and the closed tool-free Responses protocol.
The latter reported an estimated cost and is not free-cost eligibility.
OpenRouter's correctly qualified minimax/minimax-m3:free route returned 404
stating it is unavailable for free. No paid MiniMax alternative was invoked
and existing approved Combos were not edited.

No canonical cross-host bootstrap is claimed. The legacy deployer assumes
local peers and an active local target. State transfer, writer fencing, paired
cross-host recovery and real O2 supervision were requirements of that earlier
plan, not of the current single-primary RTX deployment architecture. The
source-host storage latch was subsequently cleared by the approved recovery
recorded in HomeLab; it is not a current RTX blocker. Source review, real-phone
acceptance and controlled reboot recovery remain distinct from the recorded
runtime acceptance. Do not revive O2 or alter storage guards to complete the
superseded plan.

The legacy cross-host observation adapter was validated against the then-live
source O2 baseline and independently verified candidate acceptance on its
actual host. Its
bounded freshness, persistent host/data identities, source guard and canonical
writer checks have focused negative regression coverage. It deliberately
returns `ready_for_mutation=false`; the transactional writer-fencing, final
state-transfer and paired-recovery implementation remains outstanding. This
preparation is not canonical cross-host bootstrap support.

The final saved-configuration regression covers absent live model and effort
controls plus an empty effort catalogue. Approved O3 model and effort stay
visible and read-only, using their saved values after reconnect.
