# Mac app development

## Branches and checkouts

| Role | Branch | Local checkout |
| --- | --- | --- |
| Shared integration | `main` | `/Users/Jo/GitHub/omnigent` |
| Mac integration and installed app source | `codex/mac-app` | `/Users/Jo/GitHub/omnigent-o3-routing-review-mvp` |
| First feature workspace | `codex/mac-next` | `/Users/Jo/GitHub/_worktrees/omnigent/mac-next` |

The Mac integration lane starts at `28095733f`, already published on `main`.
Future Mac features target `codex/mac-app`. This setup does not undo the O3
changes already in `main`, and does not update either server deployment.
The historical integration directory name is retained because the installed
app's `omnigent_path` points to its `.venv/bin/omnigent`.

## Start another feature

Choose a descriptive branch and directory name; for example:

```sh
git -C /Users/Jo/GitHub/omnigent-o3-routing-review-mvp worktree add \
  -b codex/mac-settings \
  /Users/Jo/GitHub/_worktrees/omnigent/mac-settings codex/mac-app
```

Work in the new directory. Install its own dependencies using CONTRIBUTING.md;
do not share an editable Python environment between source checkouts. Commit
there, push the feature branch when authorized, and open its PR against
`codex/mac-app`, not `main`:

```sh
gh pr create --base codex/mac-app --head codex/mac-settings
```

Use the repository PR template and run the applicable tests and pre-commit
checks. After a feature merges remotely, update the clean stable checkout with
`git pull --ff-only`. Preserve any local changes before updating it.
Build/package the installed Mac app from the stable integration checkout and
verify the actual installed app after an intentional update. A source merge
alone does not refresh an already running backend or the packaged Electron UI.

## Parallel work and runtime isolation

Feature worktrees support parallel editing and unit tests. Keep the everyday
installed app on the stable checkout and its existing port 6768.

`OMNIGENT_DATA_DIR` and `OMNIGENT_CONFIG_HOME` isolate some state, but native
auth-token and daemon registries still use `~/.omnigent`. The custom Electron
profile also canonicalizes local URLs to port 6768. Therefore, a separate port
and worktree are not sufficient for simultaneous full native instances.
Use unit tests or isolated frontend previews for concurrent feature work.
Full native feature testing requires a separately isolated user/runtime setup,
or a deliberate test window on the stable app with recovery prepared.
Do not copy live databases, credentials, or daemon pidfiles into a feature tree.

## Later integration with O1/O2

When explicitly requested, merge current shared changes into `codex/mac-app`,
resolve compatibility, and validate both affected Mac and server paths. Open a
reviewed PR from `codex/mac-app` to `main`. The merge publishes shared source;
server deployment is a separate authorized operation and follows the HomeLab
server-first and peer-supervision rules in AGENTS.md.

## Verify the setup

```sh
git -C /Users/Jo/GitHub/omnigent worktree list
git -C /Users/Jo/GitHub/omnigent-o3-routing-review-mvp status --short --branch
git -C /Users/Jo/GitHub/_worktrees/omnigent/mac-next status --short --branch
```
