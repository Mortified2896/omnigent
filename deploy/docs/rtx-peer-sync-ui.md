# RTX version display and token-free O2 sync

## Status and scope

This change implements the **portable, read-only core**, not a working web
button or a privileged deployment endpoint. `peer_deployer.sync_plan` provides
version display data, conservative readiness decisions, exact release/process/DB
pins, and stale-request rejection. The external host adapter and web integration
remain disabled/unimplemented until the RTX continuation below is completed.
There are no model calls, service operations, network calls or filesystem writes
in the core. A ready plan is **not** authorization to deploy.

Keep the existing two-peer architecture. First expose the O2 action in O1's
Deployment panel, so the page showing progress is not the target being stopped.
Keep the ordinary per-instance version label visible on both instances.

## Version identity

Show three distinct values:

* **Official/base Omnigent**: `upstream_version`, with the exact `upstream_ref`
  in details. Only populate from explicitly recorded, verified build provenance.
  Missing provenance means **not recorded**, not the installed package version.
* **Custom version**: `git-<12-character fork SHA>`; full SHA in details. No
  manually maintained version counter and no fabricated build ordering.
* **Installed package**: `package_version`, which may be identical across
  different customized commits. Keep this distinct from official/base version.

Also show the full release acceptance digest, schema revision, health and
observation time in details. The acceptance digest is the canonical JSON SHA256
used by `rtx_contract.canonical_digest`; it is **not** a wheel/tarball checksum.
The acceptance record's file hashes identify bytes and must be reverified by the
controller. Equal Git SHAs alone do not prove equal accepted releases.

Embed provenance during the clean build/acceptance stage. Do not query the
source checkout, package index or GitHub when rendering a running instance's
version. Never rewrite an already accepted record to add display metadata.

## Adapter contract

The trusted external controller constructs `PeerObservation` for O1 and O2,
`ControllerReadiness`, and the accepted release record. Read the installed peer
manifests and verify actual runtime identity; repository topology is not proof
of live topology. Current `rtx.py` checks `/v1/info` keys `instance_id` and
`build_sha`, and checks both server and host processes. Reuse those checks.

Map `build_sha` to `source_sha`. `generation` must bind both process incarnations
(e.g. boot identity plus server/host PIDs and start identities), not just a PID.
Bind O1's live-validation result to both its release digest and this generation.
Do not promote merely because `/health` returns 200 or a previous agent said it
worked. Version details and acceptance/check results are controller-owned data.

`plan_sync(source, target, acceptance, controller, now=...)` returns:

* `ready`: all declared safety prerequisites hold; a request can be constructed.
* `blocked`: stable reason codes explain every failed prerequisite.
* `already_current`: fresh healthy identities show the same SHA and acceptance
  digest; no mutation is offered, even when O2 is busy.

`plan.display()` supplies the panel data. `plan.request(uuid4)` supplies only a
fixed operation, expected identities, expiration and idempotency key. It carries
no selectable URL, filesystem path, shell command, force flag or arbitrary target.
The user-facing action is **Update O2 to O1's exact custom build**.

All capability flags default to false. A UI must not replace missing fields with
optimistic defaults. Observations expire after 60 seconds. Source O1 can keep
serving work; target O2 must have a proven zero active-work count. Same-schema
acceptance, distinct database identities, explicit rollback readiness, byte
verification, live O1 validation and an independent controller are required.
Matching schema strings is necessary here, but is not proof of data compatibility.

## Security and transaction integration still required

Use the existing authenticated administrator boundary. Add the appropriate
same-origin/CSRF protection for cookie-authenticated writes. Never accept
observations, acceptance contents or capability flags from a browser. Never put
a privileged generic command, sudo shell, Docker socket or Tailscale control
interface in the web application.

A worker outside **both** Omnigent service lifecycles must own the job. A FastAPI
background task, or an O2 child with a different process name, is not that worker.
The endpoint acknowledges a durable job; polling must survive the target restart.
The ordinary path must not create an AI conversation or invoke a model/provider.

Before mutation, the worker must:

1. Acquire the existing deployment-wide lock exactly once. Recollect evidence,
   reverify the accepted bytes and live O1 validation, recompute the plan, then
   call `validate_sync_request`. The pin comparison is not authentication and
   the UUID format check is not deduplication.
2. Durably reserve the idempotency key, binding it to the exact expected payload.
   Return the same job for an identical retry; reject reuse for another payload.
   Preserve unresolved transaction evidence; fail closed on unknown status.
3. Prevent new O2 work and recheck idleness before stopping it. For v1, refuse a
   busy target instead of killing work or silently waiting indefinitely.
4. Fence **all** O2 writers through startup acceptance/rollback: browser/API,
   host/runner traffic, scheduler and background jobs, not just the public proxy.
   Take a consistent backup of O2's own state. Do not copy O1's database,
   sessions, credentials, configuration, host IDs or artifact storage into O2.
5. Reuse the reviewed RTX transaction mechanism, pinning the source as well as
   target under that same lock. Do not nest another `rtx.locked()` around a
   function that already takes it; refactor a shared locked transaction body if
   needed. Recheck O1 continuity, exact candidate identity and target ownership.
6. Verify the requested build, host connectivity, schema, existing-session reads
   and rendered UI. Commit before reopening target writes. Expose durable
   progress, failure and recovery state; report rollback only after verification.

### Existing promoter must not be wired directly to HTTP

At the inspected base, `rtx.promote()` calls `stop(target)` without an admission
or idle guard. It marks `database_mutated=True` before starting the candidate;
its error path can invoke `restore_state()` using the saved DB and state archive.
That is **not** a safe one-click rollback if the new candidate has already
accepted user writes: restoring the backup could discard them. The adapter must
prove the all-writer fence and target-specific rollback compatibility, or refuse
the operation. Once writes are reopened, do not automatically restore an older DB.
A failed rollback must remain an unresolved transaction, not a success banner.

An independent deterministic controller can retain O1 as a healthy reference
without an AI supervisor task. Do not spoof an instance/supervisor identity to
bypass existing tooling. Follow `deployment-controller-scope.md` and the current
HomeLab workflow. Source code belongs here; installed units, access control and
host wiring belong in HomeLab and need separate deployment authorization.

## RTX completion / acceptance tests

Fetch current fork and HomeLab refs first, preserve dirty work, and reconcile the
exact task branch. Read current AGENTS guidance, including any documentation
cleanup that landed since this change. Do not use stale topology/ports from chat.

Complete the runtime metadata adapter, authenticated API, durable external job
worker, target admission/write fencing and UI. Keep the action disabled until
those pieces are actually installed and verified. No changes to OmniRoute,
models, scoring, Tailscale, peer topology or unrelated user sessions are needed.

Run the portable suite plus real repository lint/types/frontend tests. Add
integration tests for unauthenticated/non-admin/CSRF calls, source/target drift,
missing evidence, unhealthy peers, duplicate clicks, cross-process locking,
busy-target races, timeout, loss of controller/browser/target, supervisor restart,
artifact tampering, missing/failed backup, schema mismatch, failure before/after
mutation, failed rollback, and proof that no accepted user writes are lost.
Verify no model/provider endpoint is called on success or routine failure.
Use disposable instances/DBs and marked test chats; never fault-inject live O1/O2
or deploy merely to test this branch. Keep the PR draft until reviewed.

Portable tests (requires pytest, not a running Omnigent):

```sh
python -m pytest -q tests/deploy/test_rtx_sync_plan.py
```
