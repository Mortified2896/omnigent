# Harbor benchmark capture

Status: source implementation and isolated Mac validation, 2026-09-15.
Part of #158; continued in draft PR #159 on `codex/harbor-benchmark-capture`,
based on `codex/rtx-o1-integration`. No RTX deployment is authorized by this work.

## Ownership and configuration

```text
Omnigent-managed Codex turn
  + common task identity, input, Git start/end, runtime provenance
  + Codex's original native rollout
  -> offline candidate + SelfBench provenance JSONL
  -> SelfBench construction and base/oracle verification
  -> Harbor execution, grading, storage and ATIF interoperability
```

Codex owns the full trajectory. Omnigent records identity, exact ordinary Git
worktree state and provenance. SelfBench constructs tests/reference solutions;
Harbor runs and grades tasks. OTel supplies optional timing and correlation.
There is no new trajectory, sandbox, verifier or observability framework.

Disabled by default. Set these in the **source test runner/harness environment**:

```sh
export OMNIGENT_BENCHMARK_CAPTURE=1
export OMNIGENT_BENCHMARK_CAPTURE_DIR=/absolute/path/outside/the/worktree
```

The default is `<Omnigent data_dir>/benchmark-captures`, honoring
`OMNIGENT_DATA_DIR` and otherwise using `~/.omnigent/benchmark-captures`.
The disabled path performs no capture I/O or Git commands. The harness process
manager inherits these environment variables from its runner; the native
app-server explicitly passes the capture flag and resolved root to its hooks.
Do not change a running session's capture root between start and finish.

Resolved capture roots inside the measured worktree, including symlink aliases,
are rejected. Rejection disables that capture, not the coding task. Each UUID
directory is private (0700). Captures contain full instructions, source and
potentially sensitive native history: they remain local/private; nothing uploads
automatically. No deployment storage quota, retention service or Linux service
configuration is installed here. Inspect actual RTX storage before choosing those.

## Exact lifecycle seams

| Stage | Wrapped `codex` app-server executor | Omnigent-managed `codex-native` |
| --- | --- | --- |
| Identity | `ExecutorAdapter.run_turn` forwards scaffold `ctx.response_id` as `omnigent_turn_id`; `CodexExecutor.run_turn` allocates UUID once | Existing `codex_native_hook._main_evaluate_policy` invokes capture for `UserPromptSubmit`; native `turn_id` is the equivalent task identity |
| Start | `TurnCapture.begin` before `_ensure_app_session` and `_CodexAppServerSession.start`, before `thread/start` or `turn/start` | Hook snapshots before returning control to Codex; requires explicit hook `cwd` and `turn_id` plus bridge session/thread identity |
| Thread/turn | Bind `thread/start` response ID and `turn/start` response ID; preserve the executor's explicit observed-ID correction | Bridge thread ID plus hook turn ID; on-disk index hashes session/thread/turn, with a nonblocking process lock for duplicate submissions |
| Input | Original messages, composed system/developer instructions, tools, model, effort, cwd, permissions; exact dispatched input in `codex-input.json` | Actual submitted prompt/cwd/model/effort when present in the hook; instruction context remains in native rollout |
| End | Snapshot before yielding `TurnComplete` or `ExecutorError`; exception/finally path covers transport errors | `codex_native_forwarder._handle_terminal_turn_boundary` matches exact session/thread/turn before finalizing |
| Cancellation | Stop owned app-server before snapshot; `close` preserves trajectory before private home deletion | Interrupted terminal notification -> cancelled; app-server `close` finalizes otherwise unfinished captures after stopping the writer |

The scaffold passes the validated request-path conversation ID through `TurnContext`
independently of OTel. The adapter forwards it separately from its internal cache
key (which can be random). Missing real IDs remain null; a raw executor can also
supply an explicit message session ID. If no Omnigent response ID exists, `task_identity` uses the allocated
capture UUID. Native submissions use the native turn ID explicitly; they do not
pretend it is an Omnigent response ID. Steering stays within that native turn;
its full details remain in the native rollout, not a second event recorder.

The wrapped executor already treats a final-answer `item/completed` as terminal
without requiring `turn/completed`. Capture preserves this behavior and records
`terminal_evidence=item/completed.final_answer` and the final item ID. Native
rollout boundary/preservation fields explicitly indicate whether the later native
terminal record was durable yet. Capture does not wait indefinitely for an event
that some supported Codex builds omit.

When capture is enabled, the wrapped executor creates its temporary Codex home
outside the workspace, avoiding capture of its copied authentication/config files.
When disabled, its established workspace-local temporary-home behavior is unchanged.

## Manifest and directory contract

Example (UUID and filenames are illustrative):

```text
benchmark-captures/
  .native-index/                         # native session/thread/turn lookup + locks
  7b5532fc-7b04-4e1b-b342-bc0623021be9/
    manifest.json                       # atomically replaced
    input.json                          # original boundary input
    codex-input.json                     # wrapped app-server dispatch only
    start/
      git.json
      status.z
      staged.patch
      unstaged.patch
      untracked.tar
    end/
      git.json
      status.z
      staged.patch
      unstaged.patch
      untracked.tar
    trajectory/
      rollout-2026-09-15T00-00-00-<thread-id>.jsonl
```

`schema_version=1`. The common manifest includes:

- `capture_id`, `omnigent_session_id`, `omnigent_turn_id`, `task_identity`,
  `harness`, `repo_root`, `started_at`, `completed_at`, `duration_seconds`,
  `terminal_state` (`running`, `completed`, `failed`, `cancelled`).
- `runtime`: Omnigent version, source commit if running from a source checkout,
  and Codex version when present in native session metadata.
- `requested_model`, `observed_model`, `observed_model_source`, requested/observed
  reasoning effort. `observed_model_source=codex.turn_context` means Codex's
  reported turn model, **not proof of a gateway's actual provider execution**.
- `routing`: policy/provider/connection/canonical model/Combo/strategy/request ID/
  correlation ID/retries/fallback/O3 proposal, currently null where the boundary
  has no deterministic evidence. A requested alias is never used as provider proof.
- `start`, `end`: Git snapshot metadata and artifact hashes/sizes; `artifacts`
  holds the input file hashes. `codex_thread_id`, `codex_turn_id`, `native_rollout`
  carry harness-specific trajectory association.
- `outcome`: wrapped executor response and usage or error class/retryability;
  native terminal status/error. Tests/tool output stay in the canonical rollout.
  Final response item ID is recorded when reported. No semantic grading occurs.
- `errors`: failed capture stages and exception types, without logging prompts,
  credential-bearing subprocess stderr or filesystem paths into operational logs.

An end snapshot does not imply successful execution. A completed task does not
imply a complete capture: inspect `errors`, both snapshots, preservation and
boundary status independently. Null is unavailable evidence, never zero/false.

Future Claude/Pi adapters can call the same common `TurnCapture` layer and provide
their own trajectory association/preservation. No harness event schema is required.

## Native rollout handling

Inspected local Codex CLI: **0.153.4** (no LLM call). Existing Omnigent native
resume code and current upstream Codex protocol confirm a per-thread JSONL under
`CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl`.
Archived sessions may live under `archived_sessions`. A thread can contain many
turns. `turn_context.turn_id` and `event_msg` task start/complete/abort IDs are
explicit boundaries; older files may omit them.

The adapter searches only the known private home using the exact thread suffix,
then validates `session_meta.payload.id`. It refuses ambiguous matches instead
of choosing the newest timestamp. An explicit path is supported by the adapter,
but must remain in that known home and pass the same identity check.

One exact native file is copied per capture, with its original Harbor-compatible
filename, byte size and SHA-256. A fixed 128 MiB per-file safety bound prevents
unbounded copying; larger files are marked `over_limit` with the source path.
The copy reads only the source size measured at finalization, so append activity
cannot create an endless read. Incomplete JSONL tails are marked `partial`.
`turn_start_line`/`turn_end_line` and `boundary_status` are metadata references into
the unmodified native file; this is not a new trajectory format.

Wrapped Codex homes are deleted by normal session cleanup, so retaining only the
source path would lose evidence. Native homes are more durable but remain local
mutable state. Captures preserve the file independently of either lifecycle.
Resumed turns need prior native history, so the bounded copy includes the thread
prefix. No Desktop sessions, auth files or full home directories are copied.

## Git reconstruction

The existing snapshot primitive is retained. It captures origin, HEAD, branch or
detached state, NUL-delimited status, binary/full-index staged and unstaged patches,
and non-ignored untracked files, including binary files, nested paths and symlinks.
`git --no-optional-locks` prevents status refreshing the repository index. Diffs
disable external diff/textconv/color and force standard prefixes.

To reconstruct **in a fresh disposable clone with the recorded HEAD available**:

1. Check out the chosen snapshot's `head`; restore its branch name if needed.
2. Apply nonempty `staged.patch` with `git apply --index`.
3. Apply nonempty `unstaged.patch` with `git apply` (without `--index`).
4. Extract the trusted `untracked.tar`, preserving symlinks and modes. Python's
   `tarfile` data filter works for the tested internal relative symlinks; review
   external symlink targets rather than weakening extraction filters blindly.
5. Compare status, index blobs and worktree bytes against the snapshot/artifacts.

Start/end may have different commits or branches. Snapshots record both. The
archive is a worktree delta, not a Git-object backup: keep/fetch referenced commits
before their source objects are garbage-collected. A future dirty-state adapter
must materialize start and end in separate temporary checkouts and create reachable
base/reference commits there. It must never commit/reset the user's measured repo.

Limits: ignored files, submodule working trees, external symlink targets, LFS object
storage and filter-specific reconstruction are not archived. Unborn HEAD is rejected.
Snapshots are sequential Git reads, not an atomic filesystem snapshot: another writer
in the same workspace can invalidate exactness. UUIDs/process locks prevent capture
collisions but do not claim isolation from concurrent code edits.

## SelfBench compatibility, verified upstream

Inspected [mupt-ai/self-bench](https://github.com/mupt-ai/self-bench/tree/8376c776c29006fe56a01f3ac882f3488db8b6dc)
at `8376c776c29006fe56a01f3ac882f3488db8b6dc`.

- `src/provenance/local.ts` searches `.codex/sessions`, `.codex/archived_sessions`,
  Claude and Pi roots, matching repository worktree paths. It does not discover
  Omnigent's capture directory or private native homes automatically.
- `src/provenance/session.ts` recognizes Codex `session_meta`, `event_msg`
  user/agent messages, and `response_item` messages as fallback. It extracts user
  instructions, filters injected context and redacts recognized secret patterns.
- Discovery associates completed requests with repository history and merged PRs;
  `src/cli/run.ts` uploads provenance and pins the repository. A local session
  alone is not deterministic proof of a completed Git task.
- The README explicitly says uncommitted changes are ignored. Dirty initial or
  final state is not directly reconstructed by this ingestion path.
- Private repositories are supported with authenticated GitHub read access.
  The worker's repository access must include the referenced commits. No credentials
  are included in the capture export.
- SelfBench authors held-out tests, reference patch and pinned environment from
  the base revision, then requires base-fails/oracle-passes validation. It exports
  native Harbor tasks. Tests/reference solution are its outputs, not required
  hand-authored inputs from our recorder.

Offline export:

```sh
uv run --no-sync python -m omnigent.benchmark_capture_export \
  /absolute/capture/root/<capture-id> /absolute/new-export-directory
```

This emits `candidate.json` (normalized references, revisions, dirty flags,
construction status) and `provenance.jsonl` using SelfBench's actual
`ProvenanceMessage` contract: `sourceType`, `sessionId`, `messageIndex`, `content`.
The source session selector is the capture UUID to distinguish multiple task inputs
from one native thread; the manifest retains both actual session/thread IDs.

SelfBench's `POST /v1/provenance?runId=...` accepts this NDJSON format. Its normal
CLI does not accept `candidate.json` as a run input. The remaining adapter must
reconstruct dirty snapshots in an isolated repository and supply reachable pinned
revisions/associations to SelfBench. Export deliberately says `not_constructed`;
it does not silently discard dirty work or synthesize a verifier.

A temporary Node/tsx environment ran **actual upstream**
`extractProvenanceMessages` and `provenanceMessageSchema` against synthetic native
history and this implementation's exported record: one native prompt extracted,
one export record validated. No SelfBench runtime dependency was added. The
Temporal/Postgres/Docker/model-backed constructor was not started; this is format
compatibility, not a validated runnable benchmark.

## Harbor compatibility, verified upstream

Inspected and installed [harbor-framework/harbor](https://github.com/harbor-framework/harbor/tree/96a13544537e54be84c0f316f8c3156769380684)
at `96a13544537e54be84c0f316f8c3156769380684` in a temporary environment.
`src/harbor/agents/installed/codex.py` already implements native rollout loading,
`convert_trajectory` (Codex JSONL -> ATIF), and `atif_to_native_trajectory`.
Executed all three paths on synthetic evidence: filename accepted, two ATIF v1.7
steps emitted, native JSONL rendered back. No Omnigent ATIF converter is needed.

Harbor's task contract includes `task.toml`, `instruction.md`, `environment/`,
`tests/` and `solution/`; the verifier produces reward files under
`/logs/verifier/` (`reward.txt` or `reward.json`). Its trial artifacts preserve
agent output and trajectory. SelfBench exports this native task layout.

Keep original historical rollouts alongside the task as private provenance or in
a sibling archive. SelfBench's standard export excludes local provenance/session
records, so archive preservation is our responsibility. Harbor's load-trajectory
feature can seed history, but do **not** seed a solver evaluation with the completed
reference trajectory: it reveals the answer. Preserve it for comparison instead.
No Docker grading, full Harbor job or base/oracle task acceptance was claimed here.

## OmniRoute and OTel fidelity

The inspected executor emits requested model/effort and receives app-server events;
it does not expose HTTP response headers or an OmniRoute per-attempt provider ledger.
Native forwarder launch provenance records a configured access lane/provider and
config-based model observations; those are not proof of an actual provider attempt.
O3 decisions live outside the executor payload. Their proposal/approved fields
remain null until explicit IDs are transported across that boundary.

The OmniRoute usage/logs skill documents call/request/proxy logs and per-connection
usage. Those read APIs alone do not join an Omnigent turn to a request deterministically.
**No live OmniRoute inspection occurred.** 3.8.50 is the user-supplied runtime
version, not a fresh version check, and its exact deployed correlation fields remain
unverified. No timestamp/alias-based provider inference is used.

Smallest later correlation work, conditional on live inspection: ensure one stable
request/attempt ID is returned and recorded in OmniRoute call logs/OTel alongside
connection, canonical provider/model, retry/fallback identity; expose that same ID
in Codex's native evidence. Preserve/propagate W3C `traceparent` where supported.
An HTTP header that Codex never surfaces is insufficient on its own. No OmniRoute
code, routing, account, credentials or provider state changes belong in this PR.

Existing Omnigent telemetry scopes traces by session/response; native spans carry
Codex turn identity. Capture adds `omnigent.capture_id` to an available recording
span (including native terminal spans). Codex config population already preserves
its `[otel]` configuration. Recording succeeds without any OTel exporter or SDK.
This does not establish continuous parent/child trace propagation through OmniRoute
and its providers. PR #154 was inspected only for portable identity/preservation
concepts; its Mac recorder/storage platform is not imported into this feature.

## Failure semantics and remaining acceptance

Recorder exceptions are isolated. Failed snapshot stages remain null with errors;
end snapshot and rollout preservation are attempted independently. Capture I/O runs
off the event loop and is drained on cancellation. Native duplicate/stale/other-session
notifications cannot finalize the wrong capture; retrying errors are not terminal.
A native process close without a terminal notification is marked failed/unknown
outcome, never successful. A hard process/host kill can leave a `running` manifest;
there is no new recovery daemon. Older/untrusted hooks without explicit turn/cwd
cannot produce a native start snapshot and are not inferred from timestamps.

Historical spike: inspected three existing **Omnigent-native** rollout headers on
Mac, without altering them. None had Git provenance; one referenced a temporary
fixture workspace, two a non-repository home directory. Classification: **impossible
from those artifacts alone due to missing provenance**. This does not imply all
historical tasks are unusable. No Desktop history or active RTX data was used.

Validation commands (run from the requested Mac worktree):

```sh
uv sync --frozen --extra all --extra dev
uv run --no-sync pytest tests/test_benchmark_capture.py \
  tests/test_benchmark_capture_native.py tests/inner/test_codex_benchmark_capture.py \
  tests/inner/test_codex_executor.py tests/test_codex_native.py \
  tests/test_codex_native_app_server.py tests/test_codex_native_hook.py \
  tests/test_codex_native_forwarder.py tests/inner/test_codex_native_executor.py \
  tests/runtime/harnesses/test_executor_adapter.py tests/runtime/harnesses/test_scaffold.py \
  tests/host/test_git_worktree.py tests/test_workspace_fs.py -q
uv run --no-sync pyrefly check
git diff --check
```

The acceptance fixture drives the actual wrapped app-server event loop with mocked
transport, mutates a disposable Git repository, captures terminal outcomes, restores
start/end state, resolves native history and exports SelfBench provenance. No LLM
quota is consumed. Focused checks and staged pre-commit are used; full repo CI is not
claimed. HomeLab PR #45 is unchanged: no concrete Linux storage/service requirement
was introduced that warrants an operational deployment change.

Next action: **A — read-only live RTX inspection**. Verify actual harness/version,
hook payloads/trust, rollout persistence, available correlation evidence and `/srv`
headroom before deciding whether an externally controlled opt-in canary is ready.
No deployment, restart, merge or live mutation is part of this source task.

## RTX canary evidence — 2026-09-15

**Capture acceptance failed; original release restored.** Exactly one model task
completed through RTX Omnigent's wrapped Codex app-server. The arithmetic repair
and verifier passed, but no capture manifest or capture ID was produced.

The host service had both capture variables set. `_build_runner_env` filtered
both out before the harness started, leaving capture disabled. This PR now
allowlists those two operator settings and tests the daemon -> runner -> harness
chain for unset, disabled and enabled values while retaining secret filtering.
This correction is source-tested, not deployed or accepted by a second canary.

| Evidence | Observed result |
| --- | --- |
| Controller / target | Codex Mac app / RTX O1, VM100 on pve-gpu |
| SSH verification | Tailscale 100.88.23.2 key matched trusted Proxmox guest-agent key; ED25519 SHA256:r/eh4+4Nehvi1/j+XAoYgdATzV0ik0ZnvwTRTm5mwpE |
| Original and final release | `ec53fdba8f857def7823bf3c11573646ed023876` |
| Tested candidate | `ab088f612eca873c8480bfffe03a682edf87002e`, verified wheel/build identity |
| Capture root | `/srv/omnigent/benchmark-captures`, empty; capture disabled afterward |
| Storage | `/srv` ext4 on 256 GiB `rtx-data`; 251 GiB filesystem, 206 GiB available |
| Omnigent session | `ef6db2af0ce7430a8d009868e1b12dc1` |
| Input turn / response | `turn_cf105ddb55e245f4bc401e0c6e3c2dc6` / `resp_5b67cdaceaa34b3690b546cc` |
| Codex version / thread | 0.153.4 / `01a0a3ef-05b1-71d2-8939-26c81e7f6d54` |
| Codex turn | `01a0a3ef-05be-7221-94d6-a8da41ff6414` |
| Native salvage | 80,067 bytes, 36 valid JSONL records, explicit task start/complete, 21.665 seconds |
| Native SHA-256 | `60f2a58e80783ea374945f8be38cfb5f9f5af5af7689d6f89b02dec444be806a` |
| Capture ID / reconstruction | None; capture-based reconstruction NOT proven |
| Independent fixture checks | Committed base `716b686e41fb84b082a5fba9d2342ddac87afea3` fails; separately applying salvaged final patch passes; test bytes unchanged |
| Harbor trajectory tools | Current upstream `96a13544537e54be84c0f316f8c3156769380684`: native filename accepted, ATIF-v1.7 conversion (7 steps), native roundtrip passed |
| SelfBench | Current upstream `8376c776c29006fe56a01f3ac882f3488db8b6dc` parser recovered original prompt; static audit rejects 2 changed lines (easy minimum: 20) |
| Harbor task | Not constructed or run; fixture verification is NOT Harbor base/oracle acceptance |
| Routing / OTel | REQUEST-ONLY / NONE; requested `codex/gpt-5.5`, no deterministic capture-to-gateway join |

The native file originally lived under the fixture's
`.codex-tmp/omnigent-codex-home-5gkp3u7q/sessions/2026/09/15/` and was copied to
`/srv/omnigent/diagnostics/harbor-canary-20260915/native/` before process cleanup.
It is diagnostic salvage, not a synthesized capture. Raw private trajectory and
credentials are not committed. The fixture Git bundle and final patch are retained
alongside it. The corrected enabled path should avoid this workspace-local home.

The release-switch transaction `3c34918192744ad3ad6b0b74f2c243b2` preserved
`/srv/backups/guest-20260915T071459Z.tar.gz` (SHA-256
`9089e7362527e90eba3b5eb3154236ffd462975123c072ef2c1fe93f7f05fe13`).
The reverse transaction `380141a3064f4cebb4497afeb8439881` restored the exact
original release without restoring/overwriting the database. All three normal
services were active afterward; no Codex or canary runner processes remained.

Existing rollout footprint: 7 files, 460,050 bytes total, median 56,304 bytes,
maximum 122,685 bytes. These short sessions do not predict large-workload growth.
A planning allowance of 10–100 MiB per task including Git deltas gives approximately
1–10 GiB for 100 tasks and 10–100 GiB for 1,000; no quota or retention policy is
implemented by this estimate.

Before another canary, deploy the tested environment propagation correction and
verify the effective runner/harness environment before sending the task. A full
SelfBench proof also needs a suitable completed change meeting its minimum size,
a reachable GitHub base/reference association, and its isolated construction stack.
The current local-only tiny fixture cannot satisfy those requirements. No custom
converter, fake manifest, benchmark task, extra model attempt, PR merge, O2 revival,
or OmniRoute behavior change was used to work around these failures.
