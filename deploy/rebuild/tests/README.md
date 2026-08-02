# Lean pre-cutover acceptance

`canary-run.sh` starts the rebuilt server and host on isolated port `17670`
with a fresh temporary database. The active gate is intentionally bounded:

1. the fresh database initializes;
2. server health succeeds;
3. the host registers;
4. one standalone OpenCode session edits, commits, and pushes in a disposable
   local repository;
5. the worktree HEAD and remote branch SHA match.

Run the full lean gate with:

```sh
PATH="/home/hermes/.hermes/node/bin:$PATH" \
  deploy/rebuild/tests/canary-run.sh
```

The launcher copies the operator's non-secret OpenCode provider catalog and
mounts existing provider credentials into its temporary home. It explicitly
selects `omniroute/custom/best-coding` by default; override with
`CANARY_OPENCODE_MODEL`. No secret or temporary runtime state is written to
the repository.

Langfuse is optional and is not a deployment gate. Verity, Claude SDK, Pi,
M3 outcome scoring, route approvals, review cards, and legacy database
migration are outside this acceptance sequence.
