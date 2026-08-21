# Agent guidance

Guidance for AI agents (Claude Code, Copilot, Cursor, etc.) working in this
repository. See `CONTRIBUTING.md` for the full contributor workflow.

## HomeLab server-first boundary

This section applies to the `Mortified2896/omnigent` HomeLab deployment. Other
upstream contributors may use their normal development environments.

For HomeLab work, implementation, validation, and deployment are server-first
on `ai-control-hub`. Read
`/home/hermes/workspace/repos/HomeLab/docs/codex-server-workflow.md` before
resolving a branch or changing live state.

Before the first mutation, fetch, commit, push, merge, build, or deployment,
verify all of these identities:

- remote hostname: `ai-control-hub`;
- canonical source: `/home/hermes/workspace/repos/omnigent`;
- origin fetch and push target: `Mortified2896/omnigent`;
- worktree status and current branch; and
- the exact remote ref when the user names an existing branch.

Stop on any mismatch. Do not silently repair a branch typo, substitute a
similarly named branch from another repository, or treat a branch name or
commit message as proof of product identity.

Use an isolated Omnigent task worktree on `ai-control-hub`. A local Mac clone
is read-only orientation, not the implementation or deployment source.

The default branch, commit, pull-request, and merge target is the customized
fork `Mortified2896/omnigent`. The official `omnigent-ai/omnigent` repository
and the `upstream` remote are read-only unless the owner explicitly requests an
upstream contribution. Inspect the resolved push URL before every push.

The standalone Control Room repository is retired and is never an Omnigent
source checkout. Historical names such as `control-room-deploy` and
`/var/lib/omnigent-control-room` are deployment labels, not repository
identity.

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

## HomeLab Omnigent trusted root access

O1 and O2 are intentionally trusted administrative agents on the private
HomeLab host. They have unrestricted passwordless root access through
`sudo -n <command>`. For authorized, in-scope Omnigent work, use `sudo -n`
autonomously whenever root is useful or required. Root required is a capability,
not a blocker: do not stop, ask the user to SSH into the server, or ask the user
to run privileged commands manually merely because root is required.

Normal in-scope uses include systemd/service administration; deployment and
rollback; diagnostics and journals; filesystem ownership and permissions;
package/runtime installation; HomeLab configuration; authorized database
backup or migration; and recovery of broken O1/O2 states.

Root access does not remove the dual-instance safety invariant:

- Never let an Omnigent instance upgrade itself.
- O1 upgrades O2; O2 upgrades O1.
- Target and supervisor must differ.
- Keep the healthy supervisor available throughout an upgrade or recovery.

For destructive actions outside the requested scope—including deleting user
data, destroying VMs or storage, removing credentials, disabling recovery or
rollback, or materially widening external access—obtain explicit user
authorization first.

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
