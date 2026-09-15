# Harbor benchmark capture plan

Status: implementation started on 2026-09-15. This plan targets the active RTX Omnigent runtime and its Omnigent-managed Codex CLI / Codex App Server sessions. It does **not** require moving the current executor into Daytona, OpenShell, or another outer sandbox before capture begins.

## Decision

Use existing software for the large pieces and keep the Omnigent-specific code thin:

- **Codex native rollout/session artifacts** are the canonical full agent trajectory for Codex work.
- **Git + a small Omnigent provenance layer** records the exact software state before and after a turn.
- **OpenTelemetry** remains the cross-service correlation/timing layer for Omnigent, Codex and OmniRoute; it is not the canonical benchmark payload.
- **SelfBench** is the first-choice benchmark constructor for turning selected completed real work into validated Harbor tasks.
- **Harbor** is the canonical evaluation runner/task format and ATIF trajectory interchange layer for future replay across Codex, Claude, Pi and other harnesses.

The intended boundary is:

```text
Omnigent task
    |
    +-- original instruction/context
    +-- exact Git start state
    +-- Codex native rollout
    +-- model/reasoning configuration
    +-- OmniRoute route/provider metadata
    +-- exact Git end state + tests/outcome
    |
    v
benchmark candidate archive
    |
    v
SelfBench
    |
    v
validated Harbor task
    |
    v
Harbor replay: Codex / Claude / Pi / future harnesses
```

## Why this architecture

The current Codex executor already owns the correct lifecycle seam: one long-lived `codex app-server` subprocess per Omnigent session, with Codex threads persisted across turns. Capturing at that seam avoids separately re-instrumenting every Codex tool call.

SelfBench is used for benchmark construction rather than as the live recorder. Harbor is used for reproducible evaluation rather than as the production task store. This keeps the live runtime independent from benchmark generation and lets benchmark construction improve later without losing already-recorded work.

## Canonical capture contract

Every eligible Omnigent turn should be associated with one stable `capture_id`. The capture must be sufficient to reconstruct the task even when the repository was dirty before Codex started.

Required fields/artifacts:

### Identity and instruction

- `capture_id`
- Omnigent session ID and turn ID
- harness name (`codex` first)
- native Codex thread/session identity and rollout artifact reference
- original human instruction and any explicit task context supplied by Omnigent
- timestamps

### Start repository state

- repository root and origin
- HEAD SHA
- branch / detached state
- staged changes
- unstaged changes
- untracked files needed to reproduce the workspace
- machine-readable `git status`

A HEAD SHA alone is insufficient when the worktree is dirty. The first implementation therefore captures binary-capable staged/unstaged patches plus an archive of untracked files.

### Native trajectory

- preserve the Codex-native rollout/session artifact without converting it on the hot path
- record artifact path, byte size and digest
- preserve native model/reasoning/session identifiers when available

ATIF conversion is an export concern; Harbor can be the interoperability boundary later.

### Routing and runtime metadata

- requested model and reasoning effort
- actual model when observed
- OmniRoute request/correlation identifier when available
- canonical model / provider / route selected by OmniRoute when observed
- token/latency/retry/fallback metadata when available

Routing metadata is allowed to remain incomplete rather than guessed. OmniRoute 3.8.50 currently emits its own OTel routing events; true W3C parent/child trace propagation can be added separately.

### End state and outcome

- end HEAD / branch
- final staged/unstaged/untracked state
- final patch or commit/PR identity when one exists
- tests/commands/results surfaced by the run
- terminal success/failure/cancellation state
- later acceptance signal when available

## Storage layout

The capture store should be append-oriented and content-addressed where practical. Initial proposed shape:

```text
<benchmark-capture-root>/
  <capture_id>/
    manifest.json
    instruction.txt
    trajectory/
      codex-rollout.jsonl     # copy/link/reference, depending on native lifecycle
    start/
      git.json
      staged.patch
      unstaged.patch
      status.z
      untracked.tar
    end/
      git.json
      staged.patch
      unstaged.patch
      status.z
      untracked.tar
    routing.json
    outcome.json
```

The manifest must contain hashes for copied artifacts and must never claim completeness for missing data.

## Implementation phases

### Phase 1 — Git-state primitive

Implement a harness-neutral Git snapshot helper that records:

- origin, HEAD, branch/detached state
- `git status --porcelain=v1 -z`
- `git diff --cached --binary --full-index`
- `git diff --binary --full-index`
- `git ls-files --others --exclude-standard -z` plus the referenced untracked files in a tar archive
- hashes/sizes for each generated artifact

This helper must be read-only with respect to the repository.

### Phase 2 — Codex capture adapter

Wire the primitive into the Omnigent-managed Codex lifecycle behind an explicit opt-in feature flag/config. On each captured turn:

1. allocate `capture_id` before dispatch;
2. snapshot start Git state;
3. bind the Omnigent turn to the native Codex thread/rollout artifact;
4. snapshot end Git state when the turn terminates;
5. persist model/reasoning/outcome metadata;
6. never block the actual Codex turn because benchmark capture failed.

The Codex-specific code should only resolve native trajectory/session information. Repository capture, manifests and later exporters remain harness-neutral so Claude/Pi adapters can be added without another benchmark pipeline.

### Phase 3 — OmniRoute/OTel correlation

Record the stable IDs necessary to associate a capture with OmniRoute routing events. Keep OTel as searchable timing/routing evidence rather than the only source of benchmark data.

A later thin OmniRoute change may propagate W3C `traceparent`; that is useful but not a blocker for starting the benchmark corpus.

### Phase 4 — SelfBench / Harbor export

For selected completed captures:

- feed clean committed/PR-backed work directly into SelfBench where supported;
- reconstruct dirty-start tasks from the start-state artifacts before invoking benchmark construction;
- keep the original Codex rollout available and export/attach ATIF through Harbor when useful;
- require a verifier that checks task outcomes, not equality with the original Codex patch.

The first acceptance target is 2–3 real Omnigent→Codex tasks converted into runnable Harbor tasks with base-fails / known-solution-passes validation.

### Phase 5 — Other harnesses

Add small trajectory adapters for Claude and Pi while keeping the same capture manifest, Git-state primitive, SelfBench handoff and Harbor dataset.

## Explicit non-goals for the first implementation

- replacing the current RTX executor with Daytona/OpenShell solely for benchmark capture;
- writing a new agent trajectory standard instead of preserving native rollouts / using ATIF;
- building a custom Harbor task runner;
- building a custom verifier framework instead of using Harbor/SelfBench conventions;
- requiring OTel delivery for task execution to succeed;
- making benchmark construction synchronous with ordinary Omnigent work.

## Acceptance criteria for initial rollout

1. Capture is opt-in and failure cannot fail an Omnigent/Codex turn.
2. A dirty pre-task repository can be reconstructed byte-for-byte for tracked + non-ignored untracked files from the recorded start artifacts.
3. The corresponding Codex native rollout is linked by stable identity and digest.
4. Start/end Git artifacts and manifest hashes are internally consistent.
5. Model/reasoning metadata is recorded; missing actual route/provider data remains explicit.
6. One existing real RTX Omnigent→Codex task can be captured without changing sandbox architecture.
7. At least one captured task is successfully converted by SelfBench into a Harbor task and re-run under Harbor.

## Ownership

- `Mortified2896/omnigent`: portable capture schema, Git-state capture, Codex/other harness adapters, SelfBench/Harbor export helpers.
- `Mortified2896/HomeLab`: RTX storage path, service wiring, retention and operational recovery for the capture archive / OTel collector.
- `Mortified2896/omniroute-customizations`: only thin routing-correlation changes if stock OmniRoute cannot expose the needed stable request identity.

Do not edit an installed RTX release tree as source. Source work follows the current `codex/rtx-o1-integration` lineage until that integration lands.