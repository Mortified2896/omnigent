# Observed task evidence and benchmark fit — 11 October 2026

The available traces support evaluating repository repair and terminal execution
first, with local acceptance checks for deployment, device interaction and
editorial goals. They do not yet establish which public benchmark predicts this
owner's task completion, a calibrated benchmark mixture, or comparative model
quality. Completion remains the primary objective, ahead of speed and cost.

The current experiment is documented in
[the fixed-evaluator and public-score update](2026-10-12-fixed-evaluator-and-benchmark-predictors.md).
It tests Terminal-Bench 4 first, followed by Artificial Analysis's Coding Agent
and Intelligence indices. Published-score predictivity comes before executing
a public benchmark suite.

## Completed review coverage

Three Codex subagents produced bounded offline assessments from original owner
requests, completed answers and execution evidence. The coordinating agent
verified frozen transcript hashes, checkpoint digests, turn identities and
evidence references. A second source review expanded six deliberately selected
cases without reading the first proposals. This is targeted verification, not a
random accuracy estimate or human validation.

The inventory closed on 11 October 2026 at 15:21:46 UTC. It contains 739 Codex
root chats, 23 ZCode sessions and 12 Mac Omnigent sessions: **774 roots/sessions**.

| Partition | Inventoried | Completed checkpoints reviewed | No completed checkpoint | Excluded |
| --- | ---: | ---: | ---: | ---: |
| BlockNations | 279 | 276 | 3 | 0 |
| Article Lab / Finance | 114 | 111 | 3 | 0 |
| ZCode | 23 | 23 | 0 | 0 |
| Infrastructure and other projects, including Mac Omnigent | 358 | 337 | 13 | 8 |
| Total | **774** | **747** | **19** | **8** |

Exclusions are this active routing-research root and seven explicitly marked
Mac test sessions. Missing answers and unfinished tails do not become task
failures. All 747 selected checkpoints have validated proposals; none is pending
or has an invalid checkpoint/evidence binding.

The proposals are **404 success, 297 partial, 45 not sure and one failed**.
These are assessments of selected task spans, not whole-chat success rates.
Every proposal remains `review_source=assistant_proposal`,
`human_reviewed=false` and `score_eligible=false`. No genuine human review was
created by this work.

Long traces were inspected through selected original requests, completed answers
and bounded tool excerpts, with expanded evidence where needed. Each proposal
declares its scope, inspected indices or excerpt limits, omissions and confidence.
This does not claim that every task or every byte in a root was reviewed. The
latest checkpoint can be a small follow-up after substantial earlier work. Roots
also contain forks, repeated work, test-like greetings and different model eras;
747 is not the number of independent benchmark trials.

All 137 initial Article Lab/ZCode adapter prefixes matched their saved hashes.
One Article Lab root acquired additional messages; its assessment remains bound
to the earlier checkpoint. Four proposals initially listed later unanswered
owner requests among their evidence references. Those references were moved to
a separate validated `pending_evidence` field, preserving the completed-task
rating and the original append-only records.

The six expanded source checks retained five outcome categories and changed one
shutdown proposal from `not_sure` to `success` after inspecting the second port
probe and LaunchAgent result. The updated reasons retain missing acceptance
checks. Agreement on selected cases is not a calibrated reviewer accuracy.

## What the traces actually distinguish

| Recorded work | Evidence observed | Consequence for benchmark fit |
| --- | --- | --- |
| BlockNations AI balance and unit variety | Actual evaluation output reports 668/704 fixed-reference wins, up from 659/704, but still 0/320 first-player Hard self-play wins. The completed report also says no Archers were recruited in those games. | Repository correctness and training health leave the gameplay goal unresolved. A local simulator acceptance suite must preserve rules, weights, openings, seats, seeds and balance criteria. |
| Advanced menu spacing, entry and publication | Local desktop mouse and emulated-phone touch navigation succeeded, and deployment completed. The live browser check observed an unsigned access redirect; the acceptance receipt explicitly excludes signed-in live-menu and physical-iPhone checks. | Code, local browser behavior, deployment and signed-in/device acceptance require separate evidence. |
| Article-to-video Storybook revision | Five relevant workflow tests, three article regression checks and recorded browser interactions support script, voice, media and cut editing in the requested simulated concept. | A successful prototype supplies repository/browser evidence. Real rendering, posting, phone audio and owner visual acceptance need their own tasks and checks. |
| Medium import investigation | The latest request asks for external reports and a next-attempt investigation. Search/open outputs support a qualified research answer. Earlier automatic URL import still had missing images/headings. | Successful source synthesis and resolution of an import defect are different acceptance targets, even inside one chat. |
| Netflix PiP in ZCode | Browser evidence shows PiP active and playing. The subsequent resize/other-display complaint received instructions; the new tool output only identified the host process chain. | Browser activation was demonstrated. Native resizing, cross-display movement and permission changes remained unverified. |
| Eduroam recovery in ZCode | System logs show credential-access denial and subsequent successful authentication. Direct keychain lock state, auto-lock settings and a controlled recurrence test are absent. | Recovery evidence does not support the final's more specific auto-lock cause or future-failure prediction. A diagnosis benchmark needs evidence and causal verification, not just plausible prose. |

These examples are inspectable through the local review queue. The judgments
are historical evidence assessments; no fresh production/device acceptance was
performed for them.

## Public benchmarks to test against this evidence

**SWE-bench Pro V2 supplies a repository-work comparison.** Its public release
has 642 tasks across 11 repositories, with reference solutions, verifiers and
fresh-sandbox patch regrading. The locked agent phase is offline and has a
50-minute task budget. This method is relevant to the recorded code changes and
the need to protect authoritative acceptance tests. It does not reproduce the
owner's live deployment, signed-in production or physical-device gates.
[Official protocol](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md).

The catalog was independently counted at commit
`66f92766bba642462d4bbe5479e83f91f9211862`: 642 directories and 11 repositories.
Their counts are NodeBB 38, Ansible 82, Element Web 52, Flipt 78, Vuls 58,
Teleport 68, Open Library 82, Navidrome 52, Proton web clients 55, qutebrowser 73
and Tutanota 4. These are catalog facts, not measured overlap or success rates
for the owner's repositories. [Pinned task tree](https://github.com/scaleapi/SWE-bench_Pro-os/tree/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks).

**Terminal-Bench 4.0 is the first predictor to test**, motivated by actual
service, environment, network and recovery work in the traces and the owner's
explicit priority. Subsequent source inspection audited its 66-task manifest
and published configurations. The official leaderboard supplies documented
model/effort score matches for 133 selected checkpoints; harness and backend
equivalence remain unqualified. No full-suite 4.0 execution score was measured on
this Mac or RTX. The next test is difficulty-adjusted predictive value of the
published scores. [Official leaderboard](https://www.tbench.ai/?version=4).

**WebArena-Verified is a conditional browser component.** It supplies 812
verified tasks, a 258-task hard subset and deterministic evaluation from answers
and network traces. That method fits controlled web transactions and state
checks. The PiP and menu cases show why browser completion cannot qualify native
window behavior or physical iPhone Safari acceptance.
[Official dataset and evaluator](https://github.com/ServiceNow/webarena-verified).

**DRBench is a conditional research component.** It evaluates multi-source
research with citation and insight measures. The latest Medium investigation is
an actual example of that narrower target. Editorial taste, article adaptation,
visual approval and publishing still need local rubrics and execution checks.
[Official benchmark](https://github.com/ServiceNow/drbench).

Keep these components separate until their incremental predictive value is
measured. For a task requiring code, publication and device behavior, acceptance
requires all relevant checks; a strong component score cannot fill a missing
gate. No benchmark weights or predicted completion percentages are supplied.

## Avoiding overprediction

The Mac store has only three eligible known human outcomes: two failed and one
partial, across two model identities and access lanes. All three model joins are
session-reported attribution. Under a strict completed-task label, this slice
has no positives. It cannot support meaningful binary discrimination,
calibration or benchmark-weight estimation. The 747 new proposals do not supply
independent ground truth or historical untried-model counterfactuals.

A separate 30-root recent, project-balanced request sample was selected before
outcome inspection. It is a coverage probe, not an estimate of the owner's task
mixture. Exact first-request deduplication does not remove all semantic/fork
overlap. Capability annotations are descriptive and overlapping, not weights.

The evaluation should predeclare a small set of candidate benchmark features,
compare each with a fixed qualified baseline, and test whether combinations add
value on untouched task-family and chronological holdouts. Keep whole task/root/
fork families together. Preserve model release, effort, harness, tool access,
environment, budgets and missingness. Freeze local acceptance rules before
running paired arms; inspect uncertainty and failure reasons before admitting
any change. A feature chosen after seeing the same outcomes needs fresh testing.

## Isolated replay pilot and shadow policy

A source-only native Codex pilot replayed one historical Advisor manual-effort
repair with two effort profiles. The broken starting tree failed acceptance;
the reference passed 39 authoritative cases. Only allowlisted submitted source
files entered a fresh grading tree; agent-edited tests/configuration did not.
Both submitted patches passed all 39 cases.

One arm completed in 471.48 seconds; the other reached the 480-second cap without
a completed final, although its saved patch passed. This is one task group with
budget censoring, not evidence that the faster effort setting is better. The
longer arm's usage is unavailable, not zero. Actual per-response model identity,
subscription quota and money were not established by the CLI usage event.
The native fixture is not a fully isolated public-benchmark environment.

An older standalone CLI rejected the requested model before inference. The
bundled desktop CLI accepted it. Those availability rejections remain distinct
from task-quality failures. Existing Codex authentication was used; no separate
text judging API was configured.

`routing_paired_eval.py` now records its evaluator hash and stops the owned
process group on timeout, including a child that survives the CLI parent. This
fix is covered by a real subprocess regression; it does not retroactively add
controls to the earlier pilot.

`routing_shadow_policy.py` preserves explicit choices and active sessions,
qualifies harness/access/host/workspace, and only compares matched paired
records. Repeats collapse into task groups. Its group-count floor and corrected
sign-test threshold are provisional admission rules, not calibration. The
single tied pilot does not admit a route change. Recommendations are
`actuated=false`, with no predicted success probability. Production routing
remains unchanged.

## Review site, provenance and reproducibility

The local review site is at **http://127.0.0.1:3847/**. It shows proposed outcomes,
scope, confidence, evidence links and coverage/missingness, and filters queues
by project or review status. Human forms start at `not_sure`; a proposal is not
silently adopted as a human rating. Human revisions remain in their own journal.

Confirmed final checkpoints are enforced by source: Codex final answers, ZCode
completed non-error stop messages, and Mac terminal response-execution events.
An interim Mac assistant chunk can already have message status `completed`;
the audit now counts 18 terminal-confirmed keys rather than the original 19,
with one unfinished key excluded. Runtime-only ZCode reminders and injected
Codex catalog notices retain transcript positions without becoming user tasks.

Private manifests, frozen adapter snapshots, source hashes, read times, original
and revised proposals, skip reasons and validation summaries are under
`~/.local/share/omnigent-routing-research/corpus-review/`. The combined coverage
artifact is `corpus-review-summary.json`; the site proposal journal is
`assistant-assessments.jsonl`. Source databases are read-only. Raw historical
prompts and private runtime data are not committed.

The coordinator's private manifest is `root-verification/manifest-v5.json`.
To regenerate coverage and append validated proposals idempotently:

```sh
python scripts/routing_review_aggregate.py PRIVATE_MANIFEST PRIVATE_SUMMARY \
  --publish-to PRIVATE_PROPOSAL_JOURNAL
```

The local LaunchAgent serves immutable committed Python/HTML files.
`site-release.json` records the source commit and exact file hashes. Updating
this research site does not restart Mac Omnigent or deploy an O1 application.

Verification: 26 focused regression tests passed; Ruff formatting/checks passed.
Chrome browser QA verified structured proposal rendering, evidence controls,
review filters and badges, save/reload/revisions, synthetic exclusion and no
horizontal overflow at desktop 1280 pixels and mobile 390×844. Browser saves used
a separate QA output; all its rows are synthetic and ineligible. Genuine human
reviews remain untouched. The immutable site receives a post-update live check.
