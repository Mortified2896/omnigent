# Control Room upstream 0.9 reconciliation

This document defines how the Control Room fork is rebuilt on current upstream
Omnigent instead of carrying the historical v0.8 branch topology forward.

## Baseline

- Upstream base: `omnigent-ai/omnigent@63035f92c9b945ddf0d6f0821168e9096c61aaad`
- Previous Control Room main: `Mortified2896/omnigent@ecfda04f6178f5ea8e24a249da8f056fc5d77f5b`
- Common merge base: `cfb431c20c6bc45e76ecf5058c5b83fbac8cb311`
- Reconciliation branch: `reconcile/upstream-0.9-control-room`

The old fork and current upstream diverged after the merge base. Current
upstream contains the bulk of product evolution. Control Room therefore treats
upstream as the new source baseline and re-implements only the narrow set of
Control Room behaviors that remain required.

## Required Control Room behaviors

### 1. Dual-instance deployment and exact-artifact promotion

Keep the O1/O2 operational model, but not separate long-lived source branches.

- O2 is the first deployment/soak environment.
- O1 is the maintenance environment and may intentionally lag O2.
- Both track the same authoritative `main` lineage.
- Source deployments require an explicit full 40-character Git SHA.
- Build an immutable release artifact from that SHA.
- Promote the exact tested artifact/SHA from O2 to O1. Do not rebuild from a
  branch tip during promotion.
- Runtime roots, config roots, state roots, databases, service units, ports,
  homes, and host identities remain isolated between O1 and O2.

Target flow:

```text
upstream 0.9.x
      |
      v
Control Room main @ SHA X
      |
      v
build immutable artifact X
      |
      v
O2 production @ X
      |
      v
acceptance / soak
      |
      v
O1 maintenance @ exact same artifact X
```

### 2. Duplicate live-host identity protection

Preserve the invariant from former PR #80, adapted to current upstream host
storage and tunnel code:

- reconnect with the same host ID is allowed;
- a different host ID claiming the same `(workspace/user, advertised name)` is
  rejected while the existing host is still online and heartbeat-fresh;
- rotation to a new host ID is allowed when the previous registration is
  offline or heartbeat-stale;
- distinct host names are allowed;
- cross-owner host-ID protection already present upstream must remain intact.

The rejection must happen before the current host-store rotation path can
replace the row. Add regression tests for all cases above.

### 3. Pre-launch model picker

Do not revive the historical `omni_route_picker.py` as a second provider/model
catalog implementation. Build the Control Room behavior on upstream's current
provider resolution and `model_catalog` machinery.

Required UX/semantics:

- an inline model selector is visible in the new-session composer before the
  settings/gear dialog is opened;
- changing harness changes the catalog queried/displayed;
- Pi-native supports the picker and uses the configured gateway catalog;
- Codex shows only models compatible with the Codex harness;
- Claude shows only models compatible with the Claude harness;
- configured pinned models may be merged into the live catalog when compatible;
- the backend-designated default is shown only when a row is explicitly marked
  as the default;
- when no row is marked default, render `Harness default` rather than treating
  the first row as a fabricated default;
- selecting an explicit non-default model sends/persists `model_override`;
- choosing Default omits `model_override`;
- provider/catalog errors fail loudly and visibly. Do not silently substitute a
  different provider or pretend an empty catalog is valid.

Implementation should reuse current upstream `list_models_for_worker`, provider
resolution, model-family filtering, and live OpenAI-compatible `/v1/models`
discovery wherever possible. Add only the adapter semantics Control Room needs
for pre-launch Pi support, pinned/default handling, and fail-loud behavior.

### 4. MLflow-compatible, privacy-preserving OpenTelemetry

Keep OpenTelemetry as the integration surface. MLflow is a receiver, not an
Omnigent runtime dependency.

Required behavior:

- support trace-specific `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, falling back to
  `OTEL_EXPORTER_OTLP_ENDPOINT`;
- support OTLP HTTP/protobuf for MLflow;
- traces may be enabled without implicitly enabling metrics or logs;
- metrics default to `none` unless explicitly enabled;
- logs default to `none` unless explicitly enabled;
- content capture remains opt-in;
- with content capture disabled, exception text, status descriptions,
  `error.message`, exception events, stack traces, prompts, tool payloads and
  other user/secret-bearing content must not be exported;
- metadata-only errors retain a useful `error.type` and ERROR status;
- low-noise scope filtering must not leave exported traces permanently
  rootless/IN_PROGRESS in MLflow;
- adapt to current upstream tracing topology rather than copying old private
  `_parent` manipulation without re-validation.

Regression coverage must include privacy, trace completion/rooting, protocol and
endpoint selection, and the no-implicit-logs/metrics contract.

## Historical code that is not carried forward literally

Do not cherry-pick or preserve merely for ancestry:

- v0.8.0/v0.8.1 release/version stamps;
- historical merge/sync commits;
- the old Chat/Terminal UI revert;
- historical branch-as-product-line deployment assumptions;
- duplicate model-catalog/provider code superseded by upstream;
- obsolete routing UI/backend code superseded by upstream Smart Routing.

## Historical branches

Historical branches remain preservation material until the local workspace audit
confirms their unique commits and dirty/untracked state are archived or otherwise
accounted for. Branch cleanup is a separate operation from this reconciliation.

Task-outcome/evaluation work from the historical full OmniRoute combo branch is
not part of the first upstream reconciliation patch set. Preserve it as a
specification for a later evaluation-system port; do not mix it into the minimum
runtime/deployment migration.

## Implementation order

1. Establish this upstream-based branch with no production deployment.
2. Port/adapt duplicate live-host protection with focused tests.
3. Restore the inline pre-launch picker on top of upstream model discovery.
4. Apply the minimal MLflow/privacy telemetry adaptation.
5. Rebuild the O1/O2 exact-SHA deployment/promotion tooling for the new paths and
   source-lineage model.
6. Run unit/integration/UI tests before any live deployment.
7. Build one immutable artifact from the candidate SHA and deploy it to O2 only.
8. Run O2 acceptance including model picker, real Pi turn, host identity,
   telemetry/privacy, restart/reconnect, Git edit/commit/push, and rollback.
9. Soak O2. Only after explicit acceptance mark that exact artifact promotable.
10. Promote the exact accepted artifact to O1 during a controlled maintenance
    window.

## O2 acceptance gates

A candidate must not be promoted unless all applicable gates pass:

- O1 remains untouched during O2 deployment and acceptance;
- O2 server and host reconnect cleanly and retain the intended persistent host
  identity;
- no duplicate live host can steal the same owner/name identity;
- new-session inline picker is visible and catalog changes with harness;
- Pi can select and execute `custom/best-coding` (or the current configured
  equivalent) through the intended gateway;
- Codex and Claude model lists remain family-compatible;
- explicit model override reaches the launched session; Default produces no
  override;
- catalog/provider failures are visible and do not silently fall back;
- MLflow receives useful completed traces when tracing is enabled;
- metrics/logs are not implicitly exported to MLflow;
- metadata-only mode contains no prompt, message, tool payload, credential,
  exception message, or stack-trace leakage;
- an agent can perform a disposable Git edit, commit and push using the intended
  production credentials/workspace boundaries;
- exact deployed SHA/artifact identity is recorded;
- rollback to the previous immutable O2 release is proven.

## Main-line cutover

Only after the upstream-based candidate passes the O2 gates should the fork's
`main` be moved to the reconciled lineage. Historical refs can then be reduced
separately according to the local/GitHub preservation audit.
