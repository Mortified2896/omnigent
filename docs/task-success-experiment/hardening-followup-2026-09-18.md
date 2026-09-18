# TB4 routing and evaluation hardening follow-up

**Date:** 2026-09-18

This is the current follow-up to the dated hardening audit. The historical
audit remains unchanged; this document records the accepted runtime and the
current implementation state.

## Current runtime contract

The RTX host resolves `/usr/local/bin/codex` to Codex CLI **0.153.4**. The
repository CI dependency and lockfile now use `@openai/codex` 0.153.4 so the
app-server protocol tests exercise the accepted runtime. The separate
`/home/hermes/.local/bin/codex` 0.154.0 installation is not the resolved
runtime and is not part of this contract.

RTX OmniRoute runs the official npm `omniroute` **3.8.50** tarball (build
`dea6bb8`, SHA-256
`738c58af1faae8c57eb643a939d1191f8d7e083d9295ef61687d2bff04878c29`). The
service uses the versioned `attempt-capture.cjs` preload to write privacy-safe
gateway attempt records. The capture preload is an explicit host overlay;
the companion repository documents that overlay separately from the official
package artifact.

## Lifecycle and attribution changes

The native Codex executor waits for the exact terminal record written by the
forwarder for the accepted session, thread, and turn. It no longer emits
`TurnComplete` immediately after `turn/start` or `turn/steer`. Failed terminal
records propagate the terminal error and remain failed when duplicate or late
events arrive.

The bridge persists the logical experiment attempt ID through fresh starts,
thread rotation, resume, and active-turn updates. Response links now preserve
session, primary thread, primary turn, terminal state, requested route and
effort, token usage, and any observed provider/model/connection/call-log
identifiers. Unobserved backend and call-log fields remain null or absent;
the implementation never chooses a latest matching request.

Standalone error notifications and normal terminal boundaries share the same
durable terminal-record, usage, response-link, and review scheduling path.
The forwarder is the authoritative scheduler. The old terminal-review hook is
kept only as a compatibility module for callers that explicitly install it;
the package no longer installs a second scheduler automatically.

Automated self-review remains fail-closed. Codex 0.153.4 does not expose a
supported all-tools-disabled review contract, so prompts, approval policy,
read-only sandbox settings, and post-hoc tool detection are not used as a
substitute.

Interrupted TB4 assignment reservations are permanently blocked with a
durable reason. They cannot silently rerandomize adviser assignment or rerun
after an incomplete reservation.

## Validation

The focused backend matrix passed **514 tests with 2 warnings**. Ruff check,
Ruff format check, compileall, Pyrefly, and the all-files pre-commit suite
passed. The current branch has not been deployed to O1/O2 and no production
database, credential, or provider configuration was changed by this follow-up.

The remaining adoption blockers are the unsupported tool-free automated
review contract, nullable exact OmniRoute call-log/backend attribution until
the live capture record is correlated, and the need for a disposable live
acceptance run before treating affinity or cache lineage as measured.
