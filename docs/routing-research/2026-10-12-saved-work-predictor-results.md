# Saved-work predictor results v1 — 12 October 2026

The first difficulty-adjusted comparison does **not** select a public benchmark
or a benchmark mixture for production routing. Official Terminal-Bench 4 and the
Artificial Analysis Coding Agent Index have inconsistent incremental value
across holdouts. The Intelligence Index does not improve prediction in the
predeclared fit. These results concern provisional hindsight labels, not verified
executor success or the outcome of switching a task to another model.

Reusing completed work remains the primary evaluation stream. This study made
135 blind difficulty assessments and ordinary local statistical fits. It
generated no new solver answers and ran no Harbor or public benchmark task.
Judging existing work still consumes tokens; preserve the judgments and input
hashes rather than repeatedly reviewing an unchanged task.

## Frozen inputs and difficulty assessment

The closed corpus contains 747 selected checkpoint proposals. The exporter
recovered 419 single-request episodes, of which 239 have a declared or otherwise
unambiguous start and 180 have inferred starts reserved for sensitivity checks.
The remaining 328 checkpoints are excluded rather than assigned invented task
boundaries. This scope restriction produces a selected observational population.

Of those episodes, 135 have at least one explicit, non-estimated public score
matching both recorded model and effort. There are 132 primary-scope packets and
three sensitivity-only packets. Their project families are infrastructure/other
69, Article Lab 51, ZCode 10 and BlockNations five. These are difficulty-packet
counts, not a comparison of project success rates.

Two fresh workers used fixed **gpt-6.1-sol / max** and the frozen v1 difficulty
contract. They received requests and bounded prior visible context, with later
execution, outcome proposals, executor metadata and score values hidden. The
parent independently parsed every saved assessment and checked complete unique
case coverage, canonical packet hashes, frozen file hashes, profile/rubric,
permitted evidence indices, axis ranges and provisional provenance flags.

Difficulty bands are 19 easy, 48 moderate, 45 hard, seven very hard and 16 unknown.
There are 67 null axis values out of 810; they remain missing. Task types include
31 coding, 28 terminal, 24 mixed, 22 planning/explanation, 13 research, seven
browser/native, three editorial/visual and seven unknown.

The target remains the frozen **v0** outcome proposal. Across all 135 packets,
there are 83 completed, 43 partial, one failed and eight insufficient-evidence
proposals. The primary known-target set has 124 tasks: 80 completed, 43 partial
and one failed. It cannot support a validated three-category completion
probability model. The separate 12-case v1 outcome pilot already found two
category disagreements between fixed-profile passes; judge uncertainty is real.

## Predeclared comparison

The [frozen comparison plan](predictor-study-v1-plan.md) uses a small regularized
softmax model with six blind difficulty axes, unknown-axis count, task type,
harness and categorical configured effort. Training-only imputation and scaling
prevent test data from affecting preprocessing. The L2 penalty is fixed at ten,
with one intercept-only pseudocount per class and no hyperparameter search.

Each benchmark is added to the same context model on the same tasks and folds.
Exact shared original prompts are grouped across sessions. Holdouts are by
project family, model/effort profile and time. The time cutoff is
2026-09-18 12:22:11.600 UTC, selected from primary packet dispatch times before
filtering known outcomes. Groups crossing it are omitted from that split.

Thirty comparisons cover individual benchmarks, scope sensitivity and the
predeclared common-sample individual/joint comparisons. All 188 fits converged;
the longest took 428 iterations. These are local statistical fits, not model
task executions.

## Primary results

The table reports improvement in held-out multiclass log loss over the context
model. **Positive means better prediction; negative means worse.** These numbers
are not completion-rate percentage points. Each benchmark's rows compare its
own available population; do not rank benchmarks across different populations.

| Added published feature | Known primary tasks | Project holdout improvement | Time holdout improvement | Model/effort holdout improvement |
| --- | ---: | ---: | ---: | ---: |
| Official Terminal-Bench 4 | 25 | -0.139, n=25 | +0.062, n=8 | +0.049, n=25 |
| AA Coding Agent Index | 20 | -0.019, n=20 | Not estimable | +0.089, n=20 |
| AA Intelligence Index | 124 | -0.009, n=124 | -0.004, n=24 | -0.007, n=124 |

TB4's project holdout deterioration has a profile-cluster interval of
[-0.672, -0.006] and a task-cluster interval of [-0.292, -0.012]. Its model-profile
gain has intervals [-0.014, +0.240] and [-0.003, +0.106], respectively. The time
gain is based on eight tasks, with a task-cluster interval [-0.001, +0.126].
The direction is not stable enough to justify selecting it as a routing feature.
The official feature is the requested first test;
[published leaderboard](https://www.tbench.ai/?version=4).

The Coding Agent Index gains +0.089 on the model-profile holdout, with intervals
[+0.041, +0.245] by profile and [+0.028, +0.155] by task. Its project holdout loses
0.019, with intervals crossing zero. Its time split has only one training task,
so the fit is deliberately omitted. It remains a candidate for further
validation rather than a proven winner;
[AA methodology](https://artificialanalysis.ai/methodology/coding-agents-benchmarking).

Intelligence increments are slightly negative in all three primary holdouts,
and both interval types cross zero. This fit does not establish useful
incremental value for that composite. It also does not establish that general
intelligence is irrelevant to the owner's tasks;
[AA methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking).

The context baseline itself requires scrutiny. In the 124-task Intelligence
population, its project-holdout log loss is 0.959, worse than the class-prior
baseline's 0.898. It improves the time holdout (0.603 versus 0.657) and is nearly
tied on model-profile holdouts (0.698 versus 0.699). Blind difficulty features
alone have not solved transfer across projects.

## Common population, mixtures and scope sensitivity

Only **seven known primary tasks across three model/effort profiles** have all
three scores. They comprise five partial and two completed proposals, with no
failed example. Project holdouts can score only one task because the other fold
has insufficient training data; the time holdout has no admissible fit.

On the seven model-profile holdouts, the predeclared joint model gains 0.179
over context, versus roughly 0.078–0.085 for individual scores. The joint model's
log loss is still 1.005, worse than the class-prior baseline's 0.910. These tiny,
correlated comparisons cannot establish a useful mixture. Do not optimize
weights against them or present them as independent corroboration: both AA
composites include TB4 under their respective evaluation setups.

Including the three inferred-scope episodes changes TB4's project deterioration
from -0.139 to -0.058 and its model-profile improvement from +0.049 to +0.079.
That sensitivity is another reason to preserve the scope distinction. The
Intelligence feature remains negative in all three sensitivity holdouts.

## Interpretation and economical next steps

Public scores are retrospective features attached to older tasks; name/effort
matches do not prove equivalent backend weights or harness versions. Historical
assignment depends on difficulty and owner preferences. Six blind axes reduce
some observable confounding, but they do not recover the untried model's outcome
or establish comparable difficulty overlap. Exact-prompt grouping does not
prove independence of related incidents. The 2,000-draw cluster intervals
condition on fixed predictions and do not include all fit or judge uncertainty.

Use the archived requests, outputs and acceptance receipts as the owner's
custom evaluation dataset. Preserve this initial baseline and append newly
completed ordinary tasks under a fixed judging contract. Save a forecast before
execution when possible, then judge the outcome after completion. That improves
the ladder without buying a second solution for every task. Outcome confidence,
task difficulty and predicted completion probability remain separate quantities.

Recover scope and missing acceptance evidence from existing traces before
expanding solver runs. Use human corrections as an independent adjudication
layer, never as hidden input to the automated judge. Reserve new paired runs for
a concrete routing decision with inadequate historical overlap, using frozen
initial conditions and objective acceptance checks. A representative full-suite
benchmark matrix is not the next investment supported by this evidence.

No numerical counterfactual routing probabilities are admitted from this study.
Production routing and the disabled live scoring service remain unchanged.
The isolated predictor stores held-out probability vectors as research outputs,
with `calibrated=false`, `counterfactual_validation=false` and
`router_eligible=false`.

## Reproduction and validation

The source exporter is `scripts/routing_prediction_dataset.py`; the predictor is
`scripts/routing_outcome_predictor.py`. Frozen private inputs, blind-review
journals, pre-fit validation and results are stored in the local routing-research
directory outside Git. The plan was hashed before any fit result was generated.

The checked result artifact `predictor-results-checked.json` has SHA-256
`08c23f85ea5db4228f030415724beebedf336f7b8c4fa917e1565da5e53f73d9`.
The frozen comparison-plan digest is
`be337b1b920207884d1992aa064d812144cb19690cfa232ff5ff9ff2a49da1f4`.
Private receipts bind source, episodes, crosswalk, judging profile/contract,
packet files and review journals.

An independent calculation checked all 2,379 saved probability vectors and
their loss values, case counts and common-sample baseline identity. Style-only
lint repairs were followed by a local numerical rerun: all 30 comparison objects
and held-out probability vectors are exactly identical to the preserved first
result. No judging or solver call was repeated.

Seventeen focused exporter/predictor tests pass. They cover pre-execution view
separation, scope/executor/delegation exclusions, train-only preprocessing,
grouped/time holdouts, independent loss values, rubric/evidence/input binding,
provisional flags and ambiguous missing-score joins. A separate NumPy versus
standard-library fit agrees within 1.12e-16 in probability on a three-class
fixture. No application release is part of these offline research changes.
