# Oversight Autopilot v1 — Contract & Architecture

## 1. Purpose

The Oversight Autopilot v1 is the **contract** for a future controller that
will take a GitHub issue, run a worker to implement the requested change in
an isolated worktree, review the result, and surface a pull request for a
human to merge. This document and the `omnigent/autopilot_v1/` package
together define that contract: the state vocabulary, the legal state
transitions, the configuration shape, the write-authority boundaries, and
the in-memory data shapes that the controller and the persistence layer
will exchange.

This v1 slice is **contract-only**. It does **not** include:

- durable persistence (a database migration, schema, or rows)
- GitHub polling or webhook ingestion
- a scheduler, runner, or controller implementation
- worktree lifecycle (creation, teardown, sandboxing)
- a publication controller (PR push, label management, project fields)

All of those live in follow-up issues #18 through #26. The package is
importable but inert by default; nothing in the runtime path references it.

## 2. State machine

`OversightAutopilotState` is a `StrEnum`. Classifications are kept as
module-level frozensets so downstream code can test membership in O(1).

| State          | Description                                                              | Kind                |
| -------------- | ------------------------------------------------------------------------ | ------------------- |
| `QUEUED`       | Issue accepted for autopilot intake; no worker has claimed it yet.      | non-terminal        |
| `CLAIMED`      | Controller has claimed the issue; about to enter planning.               | non-terminal        |
| `PLANNING`     | Worker / controller is drafting a plan. May require clarification.      | non-terminal        |
| `IMPLEMENTING` | Worker is making code changes in the worktree.                           | non-terminal        |
| `TESTING`      | Worker is running the project's test suite.                              | non-terminal        |
| `REVIEWING`    | Independent reviewer agent is judging the change.                        | non-terminal        |
| `FIXING`       | Worker is addressing review or test feedback before retrying.            | non-terminal        |
| `PUBLISHING`   | Worker has finished; controller is pushing the branch and opening the PR.| non-terminal        |
| `PR_READY`     | PR is open and waiting for a human merge.                                | completion, human   |
| `BLOCKED`      | Controller is paused on a human clarification.                           | non-terminal, blocked, human |
| `FAILED`       | Terminal failure; no further transitions.                                | terminal, non-retryable |
| `DONE`         | Terminal success after human merge.                                     | terminal, completion |

Classification helpers (`is_terminal`, `is_blocked`,
`requires_human_intervention`, `is_retryable_failure`, `is_completion`)
provide a single source of truth; the controller must call them rather
than comparing strings directly.

## 3. Legal transitions

The graph below is the authoritative contract. The controller must call
`assert_legal_transition(current, next_state)` before persisting any
state change. Illegal transitions raise `IllegalTransitionError` with a
message that names the current state, the attempted next state, the legal
outbound set, and a hint to consult this document.

| Current        | Legal next states                                |
| -------------- | ------------------------------------------------ |
| `QUEUED`       | `CLAIMED`, `BLOCKED`, `FAILED`                   |
| `CLAIMED`      | `PLANNING`, `BLOCKED`, `FAILED`                  |
| `PLANNING`     | `IMPLEMENTING`, `BLOCKED`, `FAILED`              |
| `IMPLEMENTING` | `TESTING`, `BLOCKED`, `FAILED`                   |
| `TESTING`      | `REVIEWING`, `FIXING`, `BLOCKED`, `FAILED`       |
| `REVIEWING`    | `FIXING`, `PUBLISHING`, `BLOCKED`, `FAILED`      |
| `FIXING`       | `IMPLEMENTING`, `BLOCKED`, `FAILED`              |
| `PUBLISHING`   | `PR_READY`, `BLOCKED`, `FAILED`                  |
| `PR_READY`     | `DONE`, `FAILED`                                 |
| `BLOCKED`      | `CLAIMED`, `QUEUED`, `FAILED`                    |
| `FAILED`       | — (terminal)                                     |
| `DONE`         | — (terminal)                                     |

Notes on the graph:

- `TESTING -> FIXING` is the canonical path for a test failure; `TESTING
  -> FAILED` is reserved for unrecoverable test infrastructure problems.
- `REVIEWING -> FIXING` covers review feedback that requires code
  changes; `REVIEWING -> PUBLISHING` is the publication-prep handoff;
  `PUBLISHING -> PR_READY` is the post-publish state. `REVIEWING` no
  longer transitions directly to `PR_READY`.
- `FIXING -> IMPLEMENTING` lets the controller re-enter the
  implement/test loop while staying under the bounded retry counters.
- `BLOCKED` resumes to either `CLAIMED` (resume the same claim) or
  `QUEUED` (release the claim back to the backlog); the controller
  chooses.
- `PR_READY -> DONE` is the **only** path into `DONE`, and it is gated on
  a human merge confirmation supplied by a future controller
  (publication layer, #24).

## 4. Authority boundaries

`WRITE_AUTHORITY` is the whitelist table consulted by `classify_write`
and `assert_write_allowed`. Unlisted (kind, principal) pairs default to
`BLOCKED_BY_POLICY` so the table is treated as a whitelist — no
permission is granted by absence.

| Kind                          | WORKER | CONTROLLER | REVIEWER | HUMAN |
| ----------------------------- | ------ | ---------- | -------- | ----- |
| `WORKER_COMMIT_LOCAL`         | ALLOW  | BLOCK      | BLOCK    | ALLOW |
| `WORKER_BRANCH_CREATE`        | ALLOW  | BLOCK      | BLOCK    | ALLOW |
| `ISSUE_BRANCH_PUSH`           | BLOCK  | ALLOW      | BLOCK    | ALLOW |
| `PR_OPEN`                     | BLOCK  | ALLOW      | BLOCK    | ALLOW |
| `PR_CREATE`                   | BLOCK  | ALLOW      | BLOCK    | ALLOW |
| `PR_REVIEW_REQUEST`           | BLOCK  | ALLOW      | ALLOW    | ALLOW |
| `MERGE_TO_MAIN`               | BLOCK  | BLOCK      | BLOCK    | ALLOW |
| `ISSUE_STATUS_UPDATE`         | BLOCK  | ALLOW      | BLOCK    | ALLOW |
| `ISSUE_COMMENT`               | BLOCK  | ALLOW      | ALLOW    | ALLOW |
| `PR_LABEL_ADD`                | BLOCK  | ALLOW      | BLOCK    | ALLOW |
| `PROJECT_FIELD_UPDATE`        | BLOCK  | ALLOW      | BLOCK    | ALLOW |

Explicit prohibitions (also encoded as config validation):

- Workers **cannot** push branches or open PRs.
- The controller **cannot** push to the base branch (`main`).
- **Auto-merge is disabled** in v1.
- Only **humans** may merge to the base branch.

## 5. Safe defaults

`load_autopilot_v1_config({})` returns the v1 defaults, which leave the
existing omnigent runtime completely unchanged:

| Field                                  | Default | Meaning                                  |
| -------------------------------------- | ------- | ---------------------------------------- |
| `enabled`                              | `False` | Feature flag; opt-in.                    |
| `publication_enabled`                  | `False` | Gated independently; requires `enabled`. |
| `allowlisted_repositories`             | `[]`    | v1 caps this at one entry.               |
| `active_issue_limit`                   | `1`     | v1 caps this at exactly one.             |
| `base_branch`                          | `"main"`| Base branch for branches and PRs.        |
| `limits.max_retries`                   | `3`     | Retry counter cap per run.               |
| `limits.max_review_cycles`             | `2`     | Review-cycle counter cap per run.        |
| `limits.max_runtime_seconds`           | `7200`  | 2-hour wall-clock cap per run.           |
| `limits.max_cost_usd`                  | `25.0`  | USD cost cap per run.                    |
| `human_approval.require_pr_ready_human_merge` | `True` | Human must merge PR.                |
| `human_approval.require_implementer_independent_review` | `True` | Implementer cannot self-review. |
| `human_approval.require_post_publish_human_confirmation` | `True` | Post-publish confirmation required. |
| `worker_authority.workers_may_push`    | `False` | Workers may only commit locally.         |
| `worker_authority.controller_may_push_to_main` | `False` | Only humans push to the base.       |
| `worker_authority.auto_merge_enabled`  | `False` | Auto-merge is disabled.                  |

## 6. Configuration validation

`load_autopilot_v1_config(mapping)` calls
`AutopilotV1Config.model_validate(mapping)` and wraps any pydantic
`ValidationError` into an `AutopilotV1ConfigError` (a `ValueError`
subclass). The error message names the failing field and explains the
violation. Validation rules:

1. `allowlisted_repositories` length must be `≤ 1` in v1.
2. `active_issue_limit` must be exactly `1` in v1.
3. `limits.max_retries` ≥ `0`; `limits.max_review_cycles` ≥ `0`;
   `limits.max_runtime_seconds` ≥ `60`; `limits.max_cost_usd` ≥ `0`.
4. `worker_authority.workers_may_push` must be `False`.
5. `worker_authority.controller_may_push_to_main` must be `False`.
6. `worker_authority.auto_merge_enabled` must be `False`.
7. `enabled=True` requires exactly one entry in `allowlisted_repositories`.
8. `publication_enabled=True` requires `enabled=True`.
9. Repository `owner` / `repo` must be non-empty, must not start with
   `.` or `-`, and may contain only `[A-Za-z0-9._-]`.
10. `base_branch` must be non-empty.

Unknown top-level or sub-model keys are rejected (`extra="forbid"`) so
typos do not silently no-op.

## 7. Failure and blocked classifications

The contract distinguishes four kinds of off-ramps:

- `BLOCKED` is a **pause** for human clarification. The run is not
  failed; the controller can resume to `CLAIMED` or `QUEUED` once the
  human responds.
- `FAILED` is a **terminal** non-retryable failure. No outbound edges.
- `FAILED_RETRYABLE_EXHAUSTED` (a `CompletionKind`, not a state) marks a
  run whose retry or review-cycle counter hit the v1 cap before the run
  could land in a completion state. The state is still `FAILED`.
- `PR_READY_AWAITING_HUMAN_MERGE` is the natural completion of the
  controller's work, gated on a human merge that has not happened yet.

`classify_completion(status)` maps a snapshot to one of `SUCCESS`,
`BLOCKED_NEEDS_HUMAN`, `PR_READY_AWAITING_HUMAN_MERGE`,
`FAILED_NON_RETRYABLE`, `FAILED_RETRYABLE_EXHAUSTED`, or `IN_PROGRESS`.

## 8. Ownership of state

| Surface                  | Owns                          |
| ------------------------ | ----------------------------- |
| GitHub (issues, PRs, labels) | Human-visible backlog and run status. |
| Omnigent (this package)  | Durable execution state — state, retry / review counters, transitions, write decisions. |

The contract treats GitHub as the source of truth for human-visible
backlog state (issue titles, labels, comments, the PR itself). The
autopilot keeps its own execution state — current state, retry and
review counters, transition history, write decisions — and reconciles
against GitHub on each controller tick (issue #25).

## 9. Backward compatibility

The `omnigent/autopilot_v1/` package is **opt-in and isolated**:

- The package is not imported by any runtime path (no reference from
  `omnigent.runtime.prompt`, the CLI, or the harness adapters).
- `AutopilotV1Config` defaults to `enabled=False`; even if the package
  were imported, the loader returns a disabled configuration.
- `omnigent.entities.task_outcome` and the other entity / config
  surfaces are untouched. The `tests/autopilot_v1/test_backward_compat.py`
  smoke test snapshots those surfaces before and after import.

Operators must explicitly set `enabled=True` plus one allowlisted
repository before any controller starts; without that opt-in, the
feature is inert.

## 10. Worker, reviewer, controller, human authority

| Concern                                       | Worker | Controller | Reviewer | Human |
| --------------------------------------------- | :----: | :--------: | :------: | :---: |
| Commit locally to the worktree                |   ✓    |     ✗      |    ✗     |   ✓   |
| Create the issue branch (local)               |   ✓    |     ✗      |    ✗     |   ✓   |
| Push the issue branch to GitHub               |   ✗    |     ✓      |    ✗     |   ✓   |
| Open the PR                                   |   ✗    |     ✓      |    ✗     |   ✓   |
| Request a review                              |   ✗    |     ✓      |    ✓     |   ✓   |
| Comment on the issue / PR                     |   ✗    |     ✓      |    ✓     |   ✓   |
| Add labels to the PR                          |   ✗    |     ✓      |    ✗     |   ✓   |
| Update project fields                         |   ✗    |     ✓      |    ✗     |   ✓   |
| Push directly to the base branch              |   ✗    |     ✗      |    ✗     |   ✓   |
| Merge to the base branch                      |   ✗    |     ✗      |    ✗     |   ✓   |
| Trigger an auto-merge                         |   ✗    |     ✗      |    ✗     |   ✗   |

The controller coordinates but does not author code. The reviewer is an
independent LLM agent — never the same one that implemented the change.
Humans are the only principals with merge authority.

## 11. Work deferred to #18–#26

- **#18** Durable persistence (DB schema, run rows, transition log).
- **#19** Issue intake (eligibility checks, label filters, claim queue).
- **#20** Untrusted content boundary (sandboxed worktree, prompt
  hygiene for issue bodies and PR review comments).
- **#21** Worktree lifecycle (creation, teardown, isolation).
- **#22** Controller workflow (state machine driver, retries, scheduling).
- **#23** Quality gates (tests, lint, typecheck, deterministic checks).
- **#24** Publication (push, PR open, post-merge confirmation).
- **#25** Reconciliation + scheduled controller (poll, reconcile drift).
- **#26** Canary rollout (single-allowlist, single-issue-limit, then
  expand).