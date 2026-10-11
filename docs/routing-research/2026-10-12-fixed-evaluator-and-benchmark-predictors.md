# Fixed evaluator and public benchmark predictors — 12 October 2026

The next routing experiment tests whether published Terminal-Bench 4 scores add
predictive value for the owner's tasks after accounting for difficulty and
execution setup. Artificial Analysis's Coding Agent Index and Intelligence
Index follow. Completion is the objective; latency and cost are secondary.
No public benchmark execution is needed to start this predictor comparison.

The [first difficulty-adjusted saved-work comparison](2026-10-12-saved-work-predictor-results.md)
is now complete. Its holdouts do not yet select a benchmark or mixture for
production routing; the results and scope/label limits are recorded separately.

The main evaluation stream reuses completed ordinary work. Freeze real requests,
saved answers and acceptance evidence into a custom evaluation dataset; do not
pay models to solve every task again. After checking the evaluator on a small
anchor, retain one judgment per completed episode and score new ordinary work
under that same contract. Recover missing scope/context from stored traces
before spending solver tokens. Reserve new executions for specific routing
questions that observational evidence cannot answer.

## Frozen review baseline

All 747 validated checkpoint proposals from the closed corpus have been copied
into retrospective baseline v0 with a manifest and journal hash. The original
turn contexts of all three review workers record **gpt-6.1-sol / max**. Thus the
initial bulk judgments already used a consistent configured model and reasoning
setting. They did not use uniform blinded evidence packets, and several scopes
remain ambiguous. Preserve them as v0 rather than relabeling them as v1.

The [v1 evaluator contract](hindsight-evaluator-v1.md) freezes the judge profile,
rubric, task selection and input hashes. The hindsight judge sees requests,
answers and execution evidence with executor identity and previous judgments
hidden. A separate worker extracts difficulty from the pre-execution task and
context with executor identity, scores and outcomes hidden. A conditional
forecaster then sees those features plus the candidate model, effort and setup.
The forecaster cannot read hindsight evidence or labels.

Configured model identity is not an immutable backend weight receipt. Reasoning
settings do not remove generation variability. Preserve dates and available
runtime receipts; version any change to the judge, rubric, task scope or evidence
view. Keep missing evidence separate from completed, partial and failed work.
All current assessments are offline assistant proposals, not human-validated
ground truth or Omnigent live automated scores.

## Audited score coverage

The private score snapshots and explicit crosswalks bind both model and effort.
No other effort's score is borrowed, estimated Intelligence values are excluded,
missing scores remain missing, and ambiguous crosswalks are rejected. Coverage
was independently recomputed from the flat proposal/profile keys.

| Published metric | Matched checkpoints | Excluding uncertain outcome proposals | Observed model/effort profiles |
| --- | ---: | ---: | ---: |
| Official Terminal-Bench 4.0 leaderboard | 133 | 116 | 10 |
| AA Coding Agent Index v1.5 | 92 | 83 | 9 |
| AA native-agent Terminal-Bench 4.0 | 92 | 83 | 9 |
| AA Intelligence Index v4.3.2, measured values | 399 | 362 | 30 |
| AA model Terminal-Bench 4.0 with mini-swe-agent | 399 | 362 | 30 |
| AA SciCode v1.0.1 | 399 | 362 | 30 |

These counts describe available features, not benchmark predictivity, model
quality or independent trial counts. Some profiles only have uncertain outcome
proposals. The corpus has 52 observed model/effort profiles, of which 28 have
fewer than five selected checkpoints. Historical assignment favors heavier
models on harder tasks. Unadjusted associations cannot identify the better
model or select routing weights under that process.

The official TB4 snapshot contains 35 configurations across 23 named models.
The relevant published configurations cover 66 task names with five repeats per
task. Native Codex/Claude Code versions vary, and inspected source-reference
discrepancies prevent assuming byte-identical task artifacts from a leaderboard
version tag alone. [Official leaderboard](https://www.tbench.ai/?version=4).

AA's **Coding Agent Index v1.5** equally averages DeepSWE v1.1, Terminal-Bench 4.0
and SWE-Atlas-QnA. Those are native agent/model/settings configurations, not
generic model-only scores. AA's **Intelligence Index v4.3.2** includes TB4 and
SciCode at 10% each of its whole composite; its model TB4 run uses mini-swe-agent.
The native and model benchmark columns must remain separate.
[Coding Agent methodology](https://artificialanalysis.ai/methodology/coding-agents-benchmarking),
[Intelligence methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking).

For GPT-6.1 Sol / max, the official native TB4 score is about 58.18%, AA's native
Codex TB4 score is 53.03%, and AA's model/mini-swe-agent TB4 score is 56.06%.
These differences come from published configurations with different evaluation
conditions; they do not isolate a causal harness effect. They also are not the
probability that the model will complete a particular local task.
[Official leaderboard](https://www.tbench.ai/?version=4),
[AA native configurations](https://artificialanalysis.ai/agents/coding-agents),
[AA Sol effort catalog](https://artificialanalysis.ai/models/releases/gpt-6-1-sol).

AA's legacy **model Coding Index** is distinct from the Coding Agent Index.
Current public objects did not expose usable current values, and the available
documentation does not resolve its current versioned formula. Its feature stays
missing. Do not reconstruct it from TB4 and SciCode.
[Intelligence methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking),
[API schema](https://artificialanalysis.ai/api/v2/openapi).

## Attribution and scope limits

All 747 selected proposals retain verified frozen snapshot/checkpoint bindings.
For the 719 Codex checkpoints, the preceding turn context supplies configured
model/effort; using current chat settings instead would disagree for three model
and ten effort records. ZCode's 23 selected messages have linked completed
model-usage rows. The five eligible Mac checkpoints have session-reported model
attribution, without independently verified provider response identity.

Task scope needs its own qualification. In 282 proposals, the declared scope
does not include an explicit numeric starting index. A nearest-user fallback is
not proof of the original task definition. Whole prefixes also contain changed
models, changed effort and persisted child-agent work. A parent final speaker
cannot identify the executor that performed every part of the task.

The 12-case v1 pilot selects three cases from each of BlockNations, Article Lab,
ZCode and other/infrastructure work. Selection uses fixed SHA ordering among
cases with one observed scoped request, no later owner steering in the selected
span, a request of at least 80 characters, and a non-estimated public-score
candidate. Selection does not inspect outcomes. This is a selected process
pilot, not a representative sample or a probability-calibration dataset.

The first v1 outcome pass produced six completed proposals, one partial and five
insufficient-evidence cases. Independent pre-execution difficulty assessments
produced two easy, three moderate, five hard, one very hard and one unknown case;
ten individual difficulty axes remain null. The blind conditional forecaster
produced six numeric distributions and six abstentions. Only four cases have
both a numeric forecast and a known first-pass outcome, and all four outcome
proposals are completed. This overlap cannot validate multiclass discrimination,
calibration or benchmark selection. Missing context and acceptance receipts are
the immediate data-quality work, not a reason to run a larger solver suite.

The parent verified packet-file hashes, canonical per-case hashes, unique case
coverage, evidence references, candidate/difficulty bindings and probability
totals. Input views stayed separate. Prior v0 and current v1 labels differ in
scope/evidence rules; their agreement is not a judge-accuracy estimate.

A second independent outcome pass used the same configured model/effort, rubric,
packets and judging instructions (only the output destination differed). It
agreed on ten of twelve categories. The two differences were
insufficient-evidence versus completed judgments. Retain both records and flag
those cases as disputed; do not silently replace the first frozen pass or infer
83% judge accuracy from agreement without an independent target. This repeat
checked the judging process and generated no new task solutions.

## Difficulty adjustment and conditional probabilities

The forecast target is `P(completed, partial, failed | task, model/effort, setup)`.
An LLM can propose those three numbers now, but they remain uncalibrated until
their predictions are tested against outcomes not shown to the forecaster.
Hindsight uncertainty about an already observed result is a separate distribution.
Insufficient evidence remains an abstention, not a fourth execution outcome or
an invented failure.

Start with pre-execution acceptance scope, systems coupling, novelty/ambiguity,
verification burden, context dependence and known environment constraints.
Compare model profiles only in task/difficulty/setup regions with overlap.
With no overlap, historical traces cannot establish how the lighter or heavier
model would have performed on the other model's work. Use representative paired
local tasks under frozen initial conditions and acceptance checks to fill that
gap.

Compare a small regularized task/difficulty/setup predictor with that same
predictor plus TB4, then AA's two indices. Keep related tasks, forks, incidents
and escalation strategies together in held-out splits. Evaluate multiclass log
loss, Brier score and calibration with uncertainty at task-family and
model-profile grains. Current public scores applied to old tasks are a
retrospective feature study, not a historical routing-policy backtest.

Combining all three scores does not supply three independent confirmations:
both AA indices already contain TB4, under different harnesses. A combination
must improve held-out prediction beyond the difficulty/setup baseline and the
individual benchmark feature. No fitted mixture or router weights are justified
by the present coverage table or unadjusted diagnostic correlations.

## Reproducible artifacts and validation

Private manifests, packets, frozen judging instructions, source snapshots, score
rows and analysis results
are retained under `benchmark-predictivity` in the local routing-research store,
outside Git. The companion coverage notebook records the counts and the
qualification limits. The source calculator
`scripts/routing_benchmark_association.py` rejects incomplete/invalid frozen
inventories and reports missing score coverage and profile-level exploratory
associations. Its output explicitly marks difficulty adjustment as not estimated
and router selection as unqualified.

Fourteen focused calculator/aggregation tests pass. They cover effort-specific
joins, missing/estimated values, uncertain labels, mixed evidence profiles,
ambiguous crosswalk rejection and repeated tasks not becoming independent
correlation points. No full public benchmark or Harbor model task was run.
Production routing remains unchanged while this evaluator and predictor study
are being established.
