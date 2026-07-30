# Agent guidance

Guidance for AI coding agents working in this repository. See `CONTRIBUTING.md`
for the full contributor workflow.

## Committing

During iteration, run pre-commit on changed or staged files. Before opening or
updating a pull request, run the repository-required full pre-commit command and
fix every reported issue. CI runs the same checks.

## Pull requests

Use `.github/pull_request_template.md` and complete every applicable section
based on the actual diff and verification performed. Keep the template
structure intact, regardless of whether the pull request is opened through the
CLI, API, or web UI.

## Code comments

Explain non-obvious intent, invariants, and safety constraints. Prefer concise
comments, but do not remove necessary operational context merely to meet an
arbitrary line limit. Describe the current scenario rather than change history,
pull requests, or ticket numbers.

## Long-running commands and tests

Long-running work must remain observable. Never leave an agent waiting for many
minutes without live output or an explicit progress check.

- Do not pipe a long-running command directly into `tail`, `head`, command
  substitution, or another consumer that may wait for EOF before displaying
  output. In particular, avoid:

  ```bash
  some-long-command 2>&1 | tail -20
  ```

  `tail -20` cannot know the final 20 lines until the producer exits, so the
  command may appear completely silent while it is still running.

- Stream output live and keep a complete log. Preserve the original command's
  exit status:

  ```bash
  set -o pipefail
  log_file=$(mktemp)
  stdbuf -oL -eL some-long-command 2>&1 | tee "$log_file"
  status=${PIPESTATUS[0]}
  tail -n 20 "$log_file"
  exit "$status"
  ```

- Before starting work expected to take more than two minutes, state what is
  being run, the expected phases, and a rough expected duration.

- For commands expected to run longer than five minutes, use a durable log and
  inspect progress at least every one to two minutes. Useful signals include:
  log-file modification time and size, the process tree and elapsed time, CPU
  and memory use, active child processes, and durable deployment or updater
  state files.

- If a command produces no new output for several minutes, inspect whether it
  is still doing useful work, blocked on a lock or network request, waiting for
  input, finished while another process still holds the output pipe open, or
  merely buffering output.

- Use line buffering where supported, such as `stdbuf -oL -eL`, unbuffered
  Python, verbose test output, or application-specific progress flags.

- Use explicit, reasonable timeouts for ordinary tests and network operations.
  Do not blindly terminate deployment, migration, promotion, or rollback work
  at a timeout; first determine whether it is inside a critical cutover or
  recovery phase.

- When only the final lines are needed for a report, capture the complete live
  output with `tee` and run `tail` on the completed log afterward.

- Do not rerun a possibly active deployment or updater request merely because
  its original shell appears silent. First check its process, durable request
  and result state, current release, and service health.
