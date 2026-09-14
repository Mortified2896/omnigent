# Agent guidance

Guidance for AI agents (Claude Code, Copilot, Cursor, etc.) working in this
repository. See `CONTRIBUTING.md` for the full contributor workflow.

## HomeLab controller, source, and runtime boundary

This section applies to the `Mortified2896/omnigent` HomeLab deployment. Other
upstream contributors may use their normal development environments.

The Mac Codex app is the external controller. Source-only HomeLab changes may be
implemented and validated in an isolated local Mac Git worktree. Keep Omnigent
worktrees organized under:

```text
/Users/Jo/GitHub/omnigent
/Users/Jo/GitHub/_worktrees/omnigent/<task>
```

A task worktree normally checks out its own `codex/<task>` branch. Before the
first source mutation, fetch, commit, or push, verify:

- repository root and worktree status;
- origin fetch and push target `Mortified2896/omnigent`;
- current branch/base and HEAD; and
- the exact remote ref when the task depends on an existing branch.

Do not assume `main` contains the active RTX feature set while PR #156 remains
unmerged. For changes that must preserve the running RTX integration lineage,
inspect the current remote head of `codex/rtx-o1-integration` before selecting a
base.

The official `omnigent-ai/omnigent` repository and `upstream` remote are
read-only unless the owner explicitly requests an upstream contribution.
Inspect the resolved push URL before every push.

The active HomeLab Omnigent runtime is no longer on `ai-control-hub`. Current
runtime identity:

- Proxmox host: `pve-gpu`;
- guest: VM 100 `rtx-omnigent`;
- private service: `https://rtx-omnigent.taile0361b.ts.net`;
- application data/releases: under `/srv` in the guest;
- active application: RTX O1 + execution host + newer OmniRoute 3.8.50.

Old O1 and O2 on `ai-control-hub` are retired from active service: stopped,
disabled, restart-fenced, and archived. Old OpenCode Web and the old OmniRoute
gateway were removed from active paths after verified archival. Do not route new
implementation or acceptance work to the old instances because historical
guidance names them.

**Do not start, restore, recreate, or otherwise reactivate old O1/O2 merely to
satisfy a legacy deployment tool, historical runbook, or stale supervisor
requirement.** Reintroducing a second Omnigent peer is a topology change and
requires explicit owner authorization.

For live runtime work, read the current HomeLab topology/workflow documents and
perform a fresh host identity check before mutation. Source-only local work does
not require touching the running service.

Never develop by editing `/srv/omnigent/releases/*` directly. Installed releases
are immutable deployment artifacts; build/stage/deploy from a verified source
branch through the existing rollback-preserving path.

The standalone Control Room repository is retired and is never an Omnigent
source checkout. Historical names such as `control-room-deploy` and
`/var/lib/omnigent-control-room` are deployment/state labels, not repository
identity.

## HomeLab deployment safety

The current HomeLab topology is **single-primary RTX O1 under external Mac Codex
control**. There is no standing live O2 peer.

For current RTX deployments, preserve the safety properties that matter without
fabricating a supervisor: exact tested/accepted candidate identity, consistent
backups, reversible release switching, health/runtime verification, rollback
preservation, and external control. The running O1 must not autonomously
self-upgrade from inside its own task context.

The legacy `peer_deployer` and
`deploy/docs/control-room-dual-instance-upgrade-safety.md` retain a hard
TARGET/SUPERVISOR separation invariant **only when a two-peer deployment has
been deliberately provisioned and that mechanism is explicitly selected**. They
are not the default or required deployment path for the current RTX topology.
Do not choose the legacy mechanism and then revive archived O2 to make its
preflight pass.

If a procedure or old document says `TARGET=O1, SUPERVISOR=O2` is required for
current RTX deployment, treat that as stale migration-era topology unless the
owner has explicitly authorized reintroducing a second peer. Current HomeLab
`docs/omnigent-current-topology.md` and `docs/codex-server-workflow.md` are the
present-state authority.

Live deployment is distinct from source implementation. A passing build or test
suite does not authorize a restart, release switch, database replacement, or
service mutation.

## HomeLab privilege boundary

Privilege comes from current machine policy and fresh observation, not from this
document. For live RTX operations, use the normal `hermes` account and only use
`sudo -n` for narrowly scoped operations after verifying it is available and
needed. A source-only Mac task should not touch live services simply to prove
privileged access.

For destructive actions outside the requested scope—including deleting user
data, destroying VMs or storage, removing credentials, disabling recovery or
rollback, or materially widening external access—obtain explicit owner
authorization first.

## Committing

Run the `pre-commit` hook before committing (`pre-commit run --all-files`, or
let it run on staged files via `git commit`). Fix issues introduced by the task
and distinguish pre-existing repository failures rather than hiding or broadly
cleaning unrelated debt.

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
removed so it can be cleaned up when that version ships. Call out the
deprecation version in code and in the PR/commit description.

## Code comments

Keep comments short and focused on the code, not on the change history.

- **Keep them brief** — prefer one or two lines. Avoid comments longer than
  three lines; if more explanation is needed, prefer a docstring or design doc.
- **Describe the scenario, not the PR** — explain what the code handles or why
  it exists without requiring future readers to chase issue/PR numbers.

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