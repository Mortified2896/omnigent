# Agent guidance

`Mortified2896/omnigent` is the development and publication target. Ordinary
work uses the selected coding harness directly; Polly or another orchestrator
is not required. This file defines the fork's default repository workflow.

## Ordinary source work

- Read applicable agent instructions, the relevant source, and nearby tests.
  Expand inspection only when the task or evidence requires it. Do not load
  contribution archives, delivery/deployment runbooks, designs, or history as a
  startup checklist. Source-only work does not require SSH or live O1/O2 checks.
- Make the smallest coherent change. Reuse a suitable existing task worktree
  and installed tools; isolate work when branches, concurrent agents, or dirty
  files would otherwise collide. Do not bootstrap the full stack by default.
- Validate the changed behavior at the smallest useful scope. Add a focused
  regression test for a bug or a genuine coverage gap. Use integration/browser
  tests when they catch risks a unit test cannot; a small UI change does not
  automatically require E2E tests, screenshots, or recordings. Docs-only work
  normally needs a diff and relevant link/instruction checks, not an app build.
  Broaden checks for auth, security, schemas, or cross-cutting behavior; small
  diffs are not automatically low risk. Do not bypass existing hooks/checks.
- For requested application changes meant to be inspected in the running
  Omnigent UI, completion includes validation, merge to the fork's main, and
  rollout per the HomeLab release architecture (see
  `docs/omnigent-release-architecture.md` in HomeLab): build an immutable
  accepted SHA, expose it as a disposable Komodo candidate for owner
  inspection at Preview `:2222`, then promote the exact approved SHA to
  production O1 and verify `:1111`, followed by live verification and
  inspectable URLs. Legacy O2 is transitional only and is not a normal
  rollout target. Treat the implementation request as authorization for
  this development rollout unless the user asks for source-only work or says
  not to merge/deploy. Do not stop at local edits or a draft PR. A request for
  deployment in a later turn supersedes an earlier no-deploy boundary.
- Follow the requested delivery and applicable session instructions. This repo
  does not additionally require an issue, PR, independent reviewer, diagram,
  demo, or full-repository test/lint run for every task. When a PR is needed,
  report the change, checks actually run, and material risks; upstream
  contributor ceremony is not a prerequisite for ordinary fork work.
- `CONTRIBUTING.md` is a short entry point, not another mandatory read.
  `CONTRIBUTING.upstream.md` preserves the inherited guide for targeted setup
  reference or an explicitly requested upstream contribution. Do not load it
  by default; for an upstream PR, verify upstream's current requirements.
- Report what changed, exact verification, and anything unverified. A prose
  policy change does not disable CI or repository protection: report an
  enforced conflicting gate rather than bypassing it or claiming it is gone.

## Always preserve

Before source mutation, verify the repository, branch/base, worktree status, and push target. `omnigent-ai/omnigent` is read-only unless the owner explicitly requests an upstream contribution.

Do not edit installed release trees as source. Do not reactivate retired old O1/O2 without explicit owner authorization.

Preserve unrelated changes. Merge and deployment follow the requested delivery
and the application-change default above. Releases, database replacement,
retired-instance reactivation, and destructive cleanup still require explicit
owner authorization. Do not infer authorization for other environments.

Before continuing another agent's branch, fetch the current GitHub refs and fast-forward the correct task branch where possible. Compare its head and base to GitHub; do not work from chat SHAs alone, reset dirty work, or blindly merge another feature branch.

## Task-specific guidance — read only when relevant

For HomeLab deployment, read HomeLab `docs/codex-server-workflow.md` when the task touches live runtime state. Do not rely on the removed/nonexistent `docs/omnigent-current-topology.md` path or historical eval/wiring runbooks as current topology authority; discover live state on the verified host.

For task scoring and live acceptance chats, follow `docs/task-scoring-and-test-sessions.md`: human tags/comments/outcomes are not scoring-AI input; keep automated reviewers disabled until their safety and outbound-input contracts are proven. Mark test chats at creation, retain inspection evidence, and never clean up unmarked or non-manifest-owned chats.

## Deployment controller scope

Apply this section only to live deployment tasks, not source-only work.

The target topology is production O1 (`:1111`) plus disposable candidates
inspected at Preview `:2222`; legacy O2 is transitional during migration only
and disappears from the active architecture once retired. The default
promotion target is O1 only, and no new application/UI release reaches O1
before the exact artifact has been tested as a candidate and manually
inspected through Preview, unless the owner explicitly overrides this
workflow. HomeLab `docs/omnigent-release-architecture.md` is the architecture
source of truth.

Read `deploy/docs/deployment-controller-scope.md` before applying an O1/O2 deployment rule. The peer-supervision rule applies only to updates controlled from inside an Omnigent instance. Independent Codex (Mac app or CLI), ZCode, and operator/SSH sessions are external controllers: they may perform owner-authorized O1 deployments directly (and legacy O2 deployments while it still exists), without an O1/O2 supervisor task, peer approval, or a TARGET/SUPERVISOR pair. Running on the same physical server does not by itself make a controller part of O1/O2.

A Codex process launched as part of an O1/O2 task is still instance-controlled; its name is not an exemption. Where an O1 task must not restart O1 itself, use an independent external controller or another suitable healthy control context; permanent O2 is not a required architectural supervisor. Do not route an independent Codex deployment through O1/O2 merely to satisfy the self-update rule. Keep identity checks, exact artifact validation, backups, rollback, and post-deployment verification for either controller type. A peer-only tool requirement is a tooling limitation, not a blanket policy for external controllers.

Before any SSH attempt, run `hostname; id; pwd`. If the independent controller is already on `rtx-omnigent`, stay local: inspect systemd, `/srv/omnigent`, `/etc/omnigent-peers`, loopback health, and Tailscale Serve directly. Never treat an SSH/Tailscale denial to the same machine as a deployment blocker. The current `peer_deployer.rtx` command is root-only and peer-mode; if external deployment needs another path, implement/test that local path rather than SSHing away and back.

When the owner explicitly authorizes merge and deployment, finish through main and the requested live deployment; do not stop at a draft PR or repeat a superseded no-deploy instruction. Preserve unrelated work and report actual blockers rather than inventing a peer requirement.
