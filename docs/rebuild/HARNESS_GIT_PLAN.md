# Harness Git / worktree plan

## Goal

Pi, OpenCode, Codex, Claude Code, Cursor, and Hermes must be able to use
`git` normally inside isolated worktrees — and the control-room deployment
must be able to tell which worktree / branch / commit a child session
acted on. No custom commit API; no shell wrappers; no custom harness.

## 1. Upstream already provides the per-session worktree

`omnigent/server/routes/hosts.py:768-806` and
`omnigent/server/routes/_host_worktree.py` ask the host daemon to run
`git worktree add` (creating a fresh branch off the validated source
repo) and persist the resulting `worktree_path` + `branch` on the
session row. The runner's `cd` cwd is set to that worktree path, so
every subprocess the harness spawns starts inside the worktree.

This is the single mechanism the rebuild branch needs. **No code
change.**

The fork's `examples/polly/skills/fanout/SKILL.md` already documents the
"one worktree per task, one sub-agent per worktree" pattern, and the
upstream `examples/polly/config.yaml` orchestrator uses it verbatim. The
Control-Room layer only has to document the convention in
`deploy/control-room/harness/worktree-conventions.md`.

## 2. Harness capability matrix

| Harness | Git in worktree? | `git commit` from worker? | `git push` from worker? | `gh pr create` from worker? | Per-delegation worktree? |
| --- | --- | --- | --- | --- | --- |
| `claude-sdk` (Verity) | yes | yes (own commit) | yes (`gate_pushes: false`) | yes (Verity's own PR) | yes (server routes a worktree per sub-agent) |
| `codex` | yes | yes | yes | yes | yes |
| `codex-native` | yes | yes (terminal-baked) | no (terminal lifecycle only) | no | no (one terminal per session) |
| `pi` | yes | yes | yes | yes | yes |
| `pi-native` | yes | yes (terminal-baked) | no | no | no |
| `openai-agents` | yes | yes | yes | yes | yes |
| `claude-native` | yes | yes (terminal-baked) | no | no | no |
| `opencode-native` | yes | yes (terminal-baked) | no (CLI does) | no (CLI does) | no (one terminal per session) |
| `cursor` / `cursor-native` | yes | yes | yes | yes | yes |
| `hermes` / `hermes-native` | yes | yes | yes | yes | yes |
| `acp` | yes | yes | yes | yes | yes |

(All "yes" cells are upstream-true; the fork's `examples/polly/agents/*/config.yaml`
adds the harness-specific reasoning + cost policies. The rebuild branch
keeps those unchanged.)

## 3. Identity & authentication

Each worker subprocess inherits a per-spawn environment constructed by
`omnigent/runtime/harnesses/_scaffold.py` (the upstream
`register_supervised_subprocess` API). The Control-Room layer sets:

```bash
GIT_AUTHOR_NAME='${OMNIGENT_BOT_NAME}'        # default: "omnigent"
GIT_AUTHOR_EMAIL='${OMNIGENT_BOT_EMAIL}'     # default: co-author trailer
GIT_COMMITTER_NAME='${OMNIGENT_BOT_NAME}'
GIT_COMMITTER_EMAIL='${OMNIGENT_BOT_EMAIL}'
GITHUB_TOKEN='${OMNIGENT_WORKER_GH_TOKEN}'    # narrow-scope PAT, never admin
```

These are populated by `deploy/control-room/harness/env.sh` and exported
into the runner systemd unit. The `omnigent <noreply@omnigent.ai>` co-author
trailer (upstream's `OMNIGENT_BOT_SETUP` doc) is added by the polly
orchestrator at `git commit` time. No code change to the worker.

## 4. Per-delegation worktree + branch

For orchestrator-orchestrated fan-out (Verity spawning N sub-agents):

```bash
# Verity (claude-sdk) per task
git worktree add .worktrees/<task_id> -b polly/<task_id>
git -C .worktrees/<task_id> commit …
git -C .worktrees/<task_id> push origin polly/<task_id>
gh pr create --base main --head polly/<task_id> \
    --title "<task_id>" --body "…"
```

This is exactly `examples/polly/skills/fanout/SKILL.md` from upstream, no
forking needed. The Control-Room doc
`deploy/control-room/harness/worktree-conventions.md` is the single
source of truth for naming and cleanup.

## 5. Parallel-worker isolation

The fork's per-delegation worktree pattern guarantees that two
parallel sub-agents cannot write to the same path: the worktree
manages the `cwd` and the branch is the unique handle. Phase 8
acceptance test #11 ("parallel workers isolated") is satisfied by the
combination of `git worktree add` + the upstream `runner/app.py` per-
session subprocess `cwd` binding.

## 6. Acceptance

Phase 8 acceptance tests #3, #4, #5 ("Pi can edit / commit / push",
"OpenCode can do the same", "parallel workers isolated") are exercised
against a **disposable fixture repo created per test run** by
`deploy/control-room/tests/conftest.py:fixture_repo` (a `git init` +
`git checkout -b` per test). The disposable repo is the test fixture;
it is unrelated to whether the test runner itself runs on a
disposable VM. Phase D, the pre-cutover canary, runs on the
**existing Omnigent VM** (see
`PHASED_IMPLEMENTATION_PLAN.md` §D) and uses disposable fixture
repos under `/tmp/canary-fixtures/<run-id>/`. The test:

1. Spins up the upstream `omnigent` server (or calls the LLM adapter
   directly) and asks the harness to perform a one-line edit.
2. Asserts the commit lands on the test branch with the expected author.
3. Asserts `git -C .worktrees/<task> log -1 --format=%H` matches what the
   worker reported.
4. Asserts the parent agent's stream carries the child's commit SHA in
   the `git.commit.outcome` attribute (Phase 7 Langfuse spec).

## 7. Out of scope

- Building a custom commit API. The fork never had one and the upstream
  shell-out pattern is the spec-required contract.
- Maintaining a "custom commit API" endpoint in the runner. The
  sub-agent's work goes through `sys_os_shell` → `git commit` →
  `git push` exactly as a human would.
- Modifying `omnigent/host/git_worktree.py` or
  `omnigent/server/routes/_host_worktree.py`. The upstream contract is
  sufficient.
