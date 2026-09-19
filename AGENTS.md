# Agent guidance

See `CONTRIBUTING.md` for the normal contributor workflow.

For the HomeLab deployment, current topology and runtime procedure live in HomeLab `docs/omnigent-current-topology.md` and `docs/codex-server-workflow.md`; read them only when the task touches live HomeLab state.

Before source mutation, verify the repository, branch/base, worktree status, and push target. `omnigent-ai/omnigent` is read-only unless the owner explicitly requests an upstream contribution.

Do not edit installed release trees as source. Do not reactivate retired old O1/O2 without explicit owner authorization.

Preserve unrelated changes. Do not merge, deploy, release, replace databases, or perform destructive cleanup unless the task explicitly authorizes it.

Before continuing another agent's branch, fetch the current GitHub refs and fast-forward the correct task branch where possible. Compare its head and base to GitHub; do not work from chat SHAs alone, reset dirty work, or blindly merge another feature branch.

For task scoring and live acceptance chats, follow `docs/task-scoring-and-test-sessions.md`: human tags/comments/outcomes are not scoring-AI input; keep automated reviewers disabled until their safety and outbound-input contracts are proven. Mark test chats at creation, retain inspection evidence, and never clean up unmarked or non-manifest-owned chats.
