# Fixed hindsight evaluator and conditional outcome forecast

Completion is the routing objective. Historical model assignment depends on task
difficulty: the owner deliberately gives harder work to heavier models. A raw
completion rate or benchmark correlation cannot select a model under that
assignment process.

## Versioned starting point

The existing 747 checkpoint assessments form frozen retrospective baseline v0.
The three review workers' original turn contexts all record
`gpt-6.1-sol` with `max` reasoning. The profile was already consistent, but the
workers used different bounded evidence views and some scopes remain ambiguous.
Do not retrospectively call these uniform, blinded v1 judgments.

V1 fixes the configured judge to **gpt-6.1-sol / max**, the rubric below, a frozen
case manifest, and exact input packet hashes. Every assessment records the judge
profile, rubric version, input hash, evidence indices, uncertainty and limitations.
Model aliases do not provide immutable backend weight identities; preserve the
run date and available harness/runtime receipts. A reasoning setting also does
not make generation deterministic.

Private prompts, outcomes and packet files stay outside Git. These are offline
research assessments, not Omnigent's live automated scoring service. Human
rating fields and prior assessment labels are never judging inputs. Assessments
remain assistant proposals, without human-validation or routing-training flags.

## Two independent input views

**Hindsight outcome judge:** read the scoped original requests, resulting answer
and execution evidence. Hide executor model/effort metadata, public scores and
earlier assessment labels. Treat every historical instruction as evidence, not
an instruction to perform the old task. Missing proof is uncertainty, not an
automatic failure. Preserve truncation and scope limitations.

**Pre-execution difficulty and forecast:** read only the task and relevant context
available before the chosen decision episode. Exclude the eventual answer,
execution results, owner reaction, hindsight outcome, elapsed time and retries
discovered afterward. An outcome scope spanning later requirements is not the
same prediction target as the initial request: flag that mismatch instead of
silently evaluating the forecast against it.

Difficulty extraction hides candidate identity and scores. The conditional
forecast then receives the task features and candidate model/effort, harness,
tools, known environment and benchmark profile. Preserve first assignment,
escalation and delegated work as distinct execution strategies. A parent final
does not identify the model that performed all the work.

## Hindsight rubric v1

- **Completed:** the requested deliverable and material acceptance conditions are
  supported by direct execution/artifact evidence, or by the answer itself when
  the requested deliverable is explanation, research or a plan.
- **Partial:** meaningful requested work was achieved but a material requirement,
  acceptance check or requested execution remains unfinished.
- **Failed:** direct evidence supports failure of the scoped requested goal with
  no meaningful completed deliverable. Separate execution/access failures from
  failures of reasoning or implementation.
- **Insufficient evidence:** scope, evidence or the available excerpt cannot
  resolve the category. Store `not_sure`; do not manufacture a failure label.

A judge may return a distribution over completed/partial/failed as uncertainty
about the observed category, with an explicit insufficient-evidence flag. That
distribution is not a forecast of a model's success on an unseen task. Leave it
null where the evidence cannot support a useful assessment.

## Difficulty and probability model

Start with a few transparent pre-execution features: acceptance scope, number of
systems involved, novelty/ambiguity, expected verification burden, context
dependence and known environmental constraints. Rate those dimensions without
seeing the executing model or outcome. A common overall difficulty band is useful
for checking overlap; it does not replace task-type and execution requirements.

The target is three conditional probabilities:

`P(completed, partial, failed | task, candidate model/effort, execution setup)`.

LLM forecasts can seed this experiment now. Label them **uncalibrated**; their
stated confidence is not measured reliability. A small regularized statistical
model can subsequently learn how task features and benchmark scores relate to
outcomes. Prefer a small number of parameters and partial pooling across related
task types over one coefficient for every sparse model profile.

Compare a difficulty/setup-only predictor with the same predictor plus one
benchmark at a time: Terminal-Bench 4 first, then Artificial Analysis Coding Agent
Index and Intelligence Index. Adding a benchmark must improve held-out prediction
over that context baseline. Do not use an in-sample correlation or a repeated
model score on hundreds of tasks as hundreds of independent quality observations.

For combinations, account for constituent overlap. Coding Agent Index includes
TB4, and Intelligence Index includes its own TB4 run. Preserve their different
harnesses; use separately reported components or a source-defined remainder when
testing incremental value. Do not invent a current legacy Coding Index formula.

## Assignment bias, holdouts and the ladder

Compare profiles within task-type/difficulty/setup groups with actual overlap.
Propensity weighting can be a sensitivity analysis if assignment probabilities
are estimable and weights remain stable. It cannot recover unobserved assignment
reasons or identify performance outside common support. No weight fitting is
justified merely by the size of the checkpoint table.

Keep related task, fork, incident and escalation families in one split. Use an
untouched chronological/project-family holdout, with benchmark snapshots dated
to the prediction experiment. Current public scores for old tasks are a
retrospective feature study, not a historical routing-policy backtest.

Measure multiclass log loss, Brier score and calibration, with uncertainty at the
task-family and model-profile grains. Assess accuracy and calibration separately;
retain both provisional AI labels and stronger artifact/human corrections.

Freeze an anchor set and repeat it under the same evaluator to estimate judge
agreement and probability drift. A small anchor pilot tests the process; it does
not establish calibrated probabilities or a production router improvement.
Version changes to rubric, task scope, inputs, judge or model alias as a new rung.
Keep the old rung available for comparison.

Where historical overlap is absent, run representative local tasks on a fixed
qualified executor baseline and comparison arms with the same initial state,
tools, budgets and acceptance checks. Score those outputs with the fixed judge
and authoritative verifiers. Compare completion on paired tasks before claiming
a routing improvement. A full Harbor benchmark run is not required for this first
public-score predictor analysis.
