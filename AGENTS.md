# Agent guidance

Guidance for AI agents (Claude Code, Copilot, Cursor, etc.) working in this
repository. See `CONTRIBUTING.md` for the full contributor workflow.

## HomeLab server-first boundary

This section applies to the `Mortified2896/omnigent` HomeLab deployment. Other
upstream contributors may use their normal development environments.

Before HomeLab work, read the authorized private HomeLab workflow available to
the executing operator. Before the first mutation, fetch, commit, push, merge,
build, or deployment, verify the execution host, exact repository root, fetch
and push owner/repository, worktree status and branch, and any user-named remote
ref.

Stop on any mismatch. Do not silently repair a branch typo, substitute a
similarly named branch from another repository, or treat a branch name or
commit message as proof of product identity.

Use an isolated Omnigent task worktree on the authorized execution system. The
local Mac Codex application is a separate boundary and is not evidence of the
O1/O2 execution target or capability state.

The default branch, commit, pull-request, and merge target is the customized
fork `Mortified2896/omnigent`. The official `omnigent-ai/omnigent` repository
and the `upstream` remote are read-only unless the owner explicitly requests an
upstream contribution. Inspect the resolved push URL before every push.

The standalone Control Room repository is retired and is never an Omnigent
source checkout. Historical deployment labels are not repository identity.

Live/private HomeLab topology, service state, credentials, and capabilities are
`INACCESSIBLE` unless verified through an authorized HomeLab authority. Do not
guess them from public repository prose, branch names, or chat history.

## HomeLab Omnigent dual-instance safety

This repository maintains the HomeLab Omnigent dual-instance deployment
(O1 maintenance + O2 production). The hard invariant is:

> Never let an Omnigent instance upgrade itself. O1 upgrades O2;
> O2 upgrades O1. The healthy peer stays alive and supervises the
> entire operation.

Any deployment action MUST explicitly declare a target and supervisor
identity, and the two must be different instances. The deployment
tooling (`deploy/scripts/peer_deployer/`) refuses to run if target
and supervisor resolve to the same instance.

The authoritative safety contract is in
`deploy/docs/control-room-dual-instance-upgrade-safety.md`. That
document overrides any less-specific deployment wording in this
repository. Read it before any deployment work.

## HomeLab capability boundary

Ordinary O1/O2 agent and harness children must not infer root access from this
file. Machine-enforced capability configuration owns desired policy, and
authorized timestamped live evidence owns observed capability. If those
authorities are unavailable or disagree, report `INACCESSIBLE`, `UNKNOWN`, or
drift and stop before the privileged action.

Human emergency root access and controlled privileged deployment are separate
boundaries from ordinary child capability. A deployment interface may perform
only its machine-validated operations after its independent authorization and
peer-supervision gates; that does not grant a general-purpose root shell to an
ordinary coding child. The local Mac Codex application is separate again.

Never restore or assume unrestricted sudo for O1/O2 children. Root-required
work needs an authorized capability path; prose is never a bypass. The
target/supervisor inequality and destructive-action authorization gates still
apply.

## Committing

Run the `pre-commit` hook before committing (`pre-commit run --all-files`, or
let it run on staged files via `git commit`). Fix any issues it reports so the
commit lands clean — CI runs the same checks.

## Local development shortcuts

Use `just` for common tasks; run `just --list` for grouped recipes.

- `just ensure` — install/check prerequisites
- `just run-ios` / `just run-android` — build/run mobile apps
- `just dev` / `just dev-mobile` — start the omnigent dev pod
- `just electron-dev` / `just electron-build` — Electron desktop shell
- `just lint` / `just lint-all` — run pre-commit
- `just normalize-locks` — rewrite lockfile registries to PyPI/npmjs.org

## Pull requests

When you open a pull request, fill in the repo's PR template at
`.github/pull_request_template.md` (case-sensitive on Linux — note the lowercase
filename). Keep every section and checkbox row so reviewers can skim them.

- **Summary** — what changed and why.
- **Test Plan** — how you verified it.
- **Demo** — a **video or images** showing the change. Expected on contributor
  PRs for UI / frontend changes (check the "UI / frontend change" box under
  *Type of change*) so reviewers can see the new behaviour without checking out
  the branch. Use `N/A` for non-visual changes.
- **Type of change** / **Test coverage** — check all that apply (at least one
  each).
- **Coverage notes** — required if you checked "Manual verification completed"
  or "Not applicable".

Generate the description from the actual diff and this session's context — lead
with the motivation, then the change. Don't pass a `--body` that skips these
sections.

## Finishing a task

When you finish a task, print instructions to the user on how to test it: the
commands to run, the inputs to provide, or the steps to reproduce so they can
verify the result themselves. Don't leave the user guessing how to confirm the
work — tell them exactly what to do.

## Deprecating features

When deprecating a feature, note the version in which it is expected to be
removed so we can clean it up when that version ships. Call out the deprecation
version in code (e.g. a `@deprecated` tag or comment naming the target release)
and in the PR/commit description, so there's a clear marker to act on later.

## Code comments

Keep comments short and focused on the code, not on the change history.

- **Keep them brief** — prefer one or two lines. Avoid comments longer than
  three lines; if you need more, the code likely needs refactoring or a doc
  string, not a wall of inline commentary.
- **Describe the scenario, not the PR** — explain *what* the code handles or
  *why* it exists, in terms a future reader needs. Don't reference PR numbers,
  issue numbers, or ticket IDs (e.g. `#1646`, `fixes JIRA-123`); the scenario
  should be clear without chasing external links.

## Database query names

Application stores use `make_named_managed_session_maker` and give every
session a stable semantic operation name. The session-level name must describe
the caller's intent rather than repeat SQL syntax; use a nested
`query_name_scope` only when one transaction needs distinct names for important
subqueries. Because the named session covers implicit flush and commit, don't
add an explicit `flush()` only to make a query name observable.

## Framework-owned instructions

Keep runtime lifecycle and metadata instructions separate from portable agent
instructions:

- Agent-spec and per-request instructions are user-authored. Framework-owned
  instructions are additive runtime behavior and are appended after them in
  `omnigent/runtime/prompt.py`.
- Keep the canonical instruction text and lifecycle gate in the owning framework
  module. Harness adapters should only transport the composed instructions; do
  not duplicate policy across adapters or add lifecycle metadata to `AgentSpec`.
- If framework instructions grow beyond a small ordered list, introduce a
  structured `FrameworkInstructions` value at the prompt-composition boundary.
