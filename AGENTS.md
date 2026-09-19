# Agent guidance

See `CONTRIBUTING.md` for the normal contributor workflow.

For HomeLab deployment, read HomeLab `docs/codex-server-workflow.md` when the task touches live runtime state. Do not rely on the removed/nonexistent `docs/omnigent-current-topology.md` path or historical eval/wiring runbooks as current topology authority; discover live state on the verified host.

Before source mutation, verify the repository, branch/base, worktree status, and push target. `omnigent-ai/omnigent` is read-only unless the owner explicitly requests an upstream contribution.

Do not edit installed release trees as source. Do not reactivate retired old O1/O2 without explicit owner authorization.

Preserve unrelated changes. Do not merge, deploy, release, replace databases, or perform destructive cleanup unless the task explicitly authorizes it.

Before continuing another agent's branch, fetch the current GitHub refs and fast-forward the correct task branch where possible. Compare its head and base to GitHub; do not work from chat SHAs alone, reset dirty work, or blindly merge another feature branch.

For task scoring and live acceptance chats, follow `docs/task-scoring-and-test-sessions.md`: human tags/comments/outcomes are not scoring-AI input; keep automated reviewers disabled until their safety and outbound-input contracts are proven. Mark test chats at creation, retain inspection evidence, and never clean up unmarked or non-manifest-owned chats.

## Deployment controller scope

Read `deploy/docs/deployment-controller-scope.md` before applying an O1/O2 deployment rule. The peer-supervision rule applies only to updates controlled from inside an Omnigent instance. Independent Codex (Mac app or CLI), ZCode, and operator/SSH sessions are external controllers: they may perform owner-authorized deployments of O1 and O2 directly, without an O1/O2 supervisor task, peer approval, or a TARGET/SUPERVISOR pair. Running on the same physical server does not by itself make a controller part of O1/O2.

A Codex process launched as part of an O1/O2 task is still instance-controlled; its name is not an exemption. Do not route an independent Codex deployment through O1/O2 merely to satisfy the self-update rule. Keep identity checks, exact artifact validation, backups, rollback, and post-deployment verification for either controller type. A peer-only tool requirement is a tooling limitation, not a blanket policy for external controllers.

Before any SSH attempt, run `hostname; id; pwd`. If the independent controller is already on `rtx-omnigent`, stay local: inspect systemd, `/srv/omnigent`, `/etc/omnigent-peers`, loopback health, and Tailscale Serve directly. Never treat an SSH/Tailscale denial to the same machine as a deployment blocker. The current `peer_deployer.rtx` command is root-only and peer-mode; if external deployment needs another path, implement/test that local path rather than SSHing away and back.

When the owner explicitly authorizes merge and deployment, finish through main and the requested live deployment; do not stop at a draft PR or repeat a superseded no-deploy instruction. Preserve unrelated work and report actual blockers rather than inventing a peer requirement.
