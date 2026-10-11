# Saved-work predictor study v1: frozen comparison plan

This experiment reuses saved task outputs and v0 outcome proposals. It tests
incremental predictive value, beginning with official Terminal-Bench 4, then
Artificial Analysis Coding Agent Index and Intelligence Index. It generates no
new task solutions and admits no production routing policy.

## Task episodes and blind features

The exporter accepts one scoped request and its selected checkpoint. The primary
set requires a declared start or a prefix containing exactly one visible owner
request. Multiple later owner requests, mixed executor configuration and observed
delegation are excluded. Short requests requiring scope reconstruction are also
excluded. A unique latest evidence-reference fallback is sensitivity-only.

The frozen export contains 419 episodes: 239 primary and 180 sensitivity-only.
Of these, 135 have at least one documented, non-estimated score feature; 132 have
primary scope. The score-bearing packets cover Article Lab 51, BlockNations 5,
infrastructure/other 69 and ZCode 10. This is a selected observational population,
not a representative estimate of all work or project success rates.

Two fresh workers use fixed gpt-6.1-sol/max and the same v1 difficulty rubric.
They see only the request and up to four prior visible conversation items,
with per-item caps and omissions declared. Configured executor metadata, later
execution, target labels and public scores are absent. Canonical case hashes and
packet-file hashes bind all extracted features. Unknown axes remain null.

## Predictor and held-out tests

The context baseline is a three-class regularized softmax predictor using the six
blind difficulty axes, unknown-axis count, task type, harness and configured
effort. Numeric imputation and scaling use training rows only. Harness-specific
effort strings remain categorical; ordinal equivalence is not assumed. No raw
model identity, elapsed time, retries, outcome reasons or post-execution
capability annotations enter the feature matrix.

The fixed L2 penalty is 10, with one intercept-only pseudocount per class. There
is no hyperparameter search. Each score feature is compared against the context
baseline on exactly the same rows and folds. A class-prior-only baseline tests
whether context features themselves add useful information.

Repeat the three individual feature comparisons on the primary subset with all
three scores available. On that same subset, also test one predeclared joint
three-score predictor with the same penalty and holdouts. Correlated TB4
components remain correlated inputs; a joint improvement is not independent
corroboration. No hand-tuned benchmark mixture or post-result feature selection
is allowed.

Primary metric: mean multiclass log loss. Secondary metric: multiclass Brier
score, summed across completed/partial/failed. Unknown outcome targets are
missing; they do not become failures. Record class counts for every split,
especially the sparse failed category.

Holdouts are leave-project-family-out, leave-model/effort-profile-out and a
chronological split. The time cutoff is the 80th percentile of primary packet
dispatch times, selected before filtering known outcome targets. Exact shared
original prompts group related sessions. A group cannot cross training and
testing; groups spanning the time cutoff are omitted from that time split.
This grouping does not prove semantic incident independence.

Report paired improvement over the context baseline and cluster bootstrap
intervals at model/effort-profile and task-group grains: 2,000 draws, fixed seed
20261012. These intervals condition on fitted held-out predictions and do not
capture all model-fitting, scope or judge uncertainty. Multiple features and
holdouts are exploratory comparisons, not independent confirmations.

## Interpretation and next decision

The target is a provisional hindsight label, so a useful feature predicts that
label before it proves real executor success. Current public scores for old tasks
make this a retrospective feature study, not a historical routing backtest.
Configuration-name matches do not establish backend/harness equivalence.

Do not rank scores from comparisons on different task populations. Do not choose
an alternate executor merely because a conditional model extrapolates a favorable
number. Historical assignment depends on difficulty and unobserved preferences;
the untried-model outcome remains unobserved. A favorable result selects a
candidate for stronger prospective validation, not automatic activation.

If context or score improvements are unstable, recover missing evidence and
evaluate subsequent ordinary work under the frozen contract. Reserve a new
paired execution for a specific consequential decision with inadequate overlap.
Keep the live scoring service and production routing unchanged during this
offline study.
