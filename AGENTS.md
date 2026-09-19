# Agent guidance

See `CONTRIBUTING.md` for the normal contributor workflow.

For the HomeLab deployment, current topology and runtime procedure live in HomeLab `docs/omnigent-current-topology.md` and `docs/codex-server-workflow.md`; read them only when the task touches live HomeLab state.

Before source mutation, verify the repository, branch/base, worktree status, and push target. `omnigent-ai/omnigent` is read-only unless the owner explicitly requests an upstream contribution.

Do not edit installed release trees as source. Do not reactivate retired old O1/O2 without explicit owner authorization.

Preserve unrelated changes. Do not merge, deploy, release, replace databases, or perform destructive cleanup unless the task explicitly authorizes it.

## Deployment controller scope

Read `deploy/docs/deployment-controller-scope.md` before applying an O1/O2 deployment rule. The peer-supervision rule applies only to updates controlled from inside an Omnigent instance. Independent Codex (Mac app or CLI), ZCode, and operator/SSH sessions are external controllers: they may perform owner-authorized deployments of O1 and O2 directly, without an O1/O2 supervisor task, peer approval, or a TARGET/SUPERVISOR pair. Running on the same physical server does not by itself make a controller part of O1/O2.

A Codex process launched as part of an O1/O2 task is still instance-controlled; its name is not an exemption. Do not route an independent Codex deployment through O1/O2 merely to satisfy the self-update rule. Keep identity checks, exact artifact validation, backups, rollback, and post-deployment verification for either controller type. A peer-only tool requirement is a tooling limitation, not a blanket policy for external controllers.

When the owner explicitly authorizes merge and deployment, finish through main and the requested live deployment; do not stop at a draft PR or repeat a superseded no-deploy instruction. Preserve unrelated work and report actual blockers rather than inventing a peer requirement.
