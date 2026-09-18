# Agent guidance

Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, commit sign-off and PR
requirements, and any applicable nested `AGENTS.md` before editing.

`Mortified2896/omnigent` is our customized fork and default publication target.
`omnigent-ai/omnigent` is upstream/read-only unless explicitly requested. Verify
the repository root, fetch and every push URL, exact branch/base and dirty state
before mutation or publication. Preserve unrelated changes.

Implement in an isolated task branch/worktree, never the canonical clean clone,
an installed release tree, or another agent's working directory. Separate agents
and tasks use separate worktrees; synchronize source/history through GitHub.
Use relevant contributor checks and report unavailable checks explicitly.

Source work may continue from any trusted development machine, including a Mac
when the preferred server is unavailable. Before work depends on live HomeLab
state, read the [HomeLab environment guide](https://github.com/Mortified2896/HomeLab/tree/main/docs/environments), resolve its
[current registry](https://github.com/Mortified2896/HomeLab/blob/main/docs/environments/current.json) and perform its live identity gate. Do not infer
live acceptance from repository tests, historical records or a successful build.

O1/O2 are runtime/deployment peers, not competing source repositories. An instance
never upgrades itself: TARGET and SUPERVISOR must differ, with a healthy real
peer supervising throughout. Preserve the [peer deployment contract](deploy/docs/rtx-peer-v2.md),
immutable artifact acceptance, state isolation, transaction and rollback safety.
A controller UI or source worktree is not a substitute runtime peer.

Merges, deployments, releases, restarts, credential/database changes and
destructive cleanup require explicit authorization. Never reactivate retired
services to satisfy a preflight. Application behavior/transaction semantics live
here; HomeLab owns current host/path/port inventory and installed host wiring.
