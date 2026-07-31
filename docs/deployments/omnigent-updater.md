# External self-update controller (issue #38)

This document describes the architecture of the external, rollback-safe
self-update controller for Omnigent. It is the canonical reference for
the `omnigent.updater` package, its state machine, the
`omnigent-updater` operator CLI, the `omnigent_updater.sh` shell
wrapper, and the systemd unit template that ships with the project.

## Why a separate updater

`scripts/promote_release.sh` is the canonical, immutable, agent-safe
release-promotion pipeline. It is reused verbatim — including the
build, manifest, provenance, preflight, and canary phases — by the
external controller so the upgrade path stays a single audited
implementation. The controller owns everything around that pipeline:

* durable request and result records,
* strict target and lineage validation,
* maintenance / drain integration with the running web service,
* migration rehearsal and backup orchestration,
* health-gated cutover and rollback,
* post-restart result reconciliation and exactly-once delivery to the
  originating conversation.

The controller never imports Omnigent runtime code from the release it
is about to replace. Its installation is durable:

```text
/opt/omnigent/updater/                      # updater install root (durable)
    venv/                                   # release-local frozen venv
    src/                                    # checked-out tree
    bin/omnigent-updater                    # CLI entry point
    bin/omnigent-updater-install.sh         # install helper
    etc/omnigent-updater.service.template   # systemd unit template
    etc/omnigent-updater.env                # optional env overrides
    etc/sudoers.d/omnigent-updater          # tightly scoped sudoers rule

/var/lib/omnigent/updates/                  # durable state root
    requests/<request_id>.json              # pending + historical requests
    running/<request_id>.json               # checkpoint while running
    results/<request_id>.json               # terminal result records
    events/<request_id>.jsonl               # append-only event history
    locks/<request_id>.lock                 # single-active-update lock

/etc/systemd/system/omnigent-updater.service   # activated by the operator
```

The state root is overridable via `OMNIGENT_UPDATER_STATE_ROOT`; the
test suite and the staging acceptance harness set this to a tmpdir so
they never touch `/var/lib/omnigent/updates`.

The updater process **does not** activate itself in production during
this work. The systemd unit template ships, but the production unit
is installed only after #29 closes (per issue #38's "blocked by"
note). Until then, every update is operator-invoked.

## State machine

The state machine is exhaustive and explicit. Rollback is a distinct
phase — not a generic failure — so monitoring can alarm separately on
`rollback_failed`.

```text
queued ─▶ validating ─▶ building ─▶ draining ─▶ rehearsing_migration
                                          │
                                          ▼
                                     backing_up ─▶ promoting
                                                       │
                                                       ▼
                                                   verifying
                                                       │
                              ┌────────────────────────┼────────────────────────┐
                              ▼                        ▼                        ▼
                          succeeded                 failed                     (no
                                                                          direct path)
                                                  │
                                                  ▼
                                              rolling_back
                                                  │
                                                  ▼
                                              rolled_back
                                                  (or rollback_failed)

queued ─▶ rejected                       # any validation failure
```

Terminal states are: `succeeded`, `rejected`, `failed`, `rolled_back`,
`rollback_failed`. Each terminal state is durable; the same request
never re-enters a non-terminal state.

## Request schema

Every request carries:

```text
request_id            # 26-char ULID; durable primary key
target_sha            # 40 lowercase hex characters
expected_current_sha  # 40 lowercase hex characters; live read on validation
origin_session_id     # conversation that originated the request
origin_conversation_id # conversation id if distinct from session id
requested_by          # "operator:<user>" or "verity" or "system"
created_at            # RFC3339 timestamp
authorization         # { kind: "operator", operator: "user", ... }
notes                 # free-form operator notes (audit only)
```

The request interface rejects any extra fields silently — only the
narrow schema above is accepted. Arbitrary shell commands, service
names, paths, remotes, scripts, and environment overrides are
**never** accepted.

## Result schema

Every terminal result carries:

```text
request_id             # foreign key back to the request
final_status           # succeeded | rejected | failed | rolled_back | rollback_failed
target_sha             # 40 lowercase hex
previous_sha           # sha that was live when the request was filed
deployed_sha           # sha currently live after the result was recorded
started_at             # RFC3339 timestamp when validation succeeded
finished_at            # RFC3339 timestamp when the terminal state was reached
failure_phase          # phase that produced the failure (empty on success)
failure_reason         # short string; never contains secrets
rollback_performed     # bool
rollback_result        # succeeded | failed | (empty)
notification_status    # pending | delivered | failed | not_applicable
```

## Validation

Validation runs **before** any build phase and fails fast. Each
rejection is recorded durably with a precise reason.

* `target_sha` must match `^[0-9a-f]{40}$`.
* `expected_current_sha` must match `^[0-9a-f]{40}$`.
* The target commit must exist on the approved writable fork
  (`Mortified2896/omnigent`); the controller shells out to
  `git -C $REPO_ROOT cat-file -t <sha>` rather than trusting any
  caller-supplied remote.
* `git merge-base --is-ancestor <lineage_anchor> <target_sha>` must
  succeed; the lineage anchor is configured via
  `OMNIGENT_UPDATER_LINEAGE_ANCHOR` (default
  `c1f23749c4dd0b24ce62a17d926b9660bf99db5c`, the post-#38 lineage
  head agreed in fork/main).
* The live SHA, read from `~/.omnigent/deployed-sha`, must equal
  `expected_current_sha`. A mismatch means the request is stale.
* The request must not already be in a terminal state.
* No conflicting production update may be active (single-active-update
  lock).

If any check fails the request is rejected and **never** proceeds to
the build phase. The controller does not silently substitute
`fork/main`, the previous release, or any other SHA.

## Maintenance and drain

The web service exposes a maintenance-mode HTTP endpoint and reads a
durable maintenance-marker file on every startup. The controller
flips the marker file, polls the web service's drain status, and only
proceeds with cutover once the web service reports no active
sessions / runners. Active sessions are **not** killed; they are
allowed to finish or be aborted explicitly by the operator.

The marker file lives at `<state_root>/maintenance.json` so it
survives web-service restarts. The web service clears the marker on
every startup unless the updater is still running; the updater
clears or reconciles the marker on success, rejection, failure,
rollback, and on its own restart.

## Migration rehearsal

The controller inspects the live database revision with the existing
helper `omnigent.db.utils._get_current_db_revision` and the candidate
head with `_get_head_db_revision`. When the candidate head differs
from the live revision, the controller:

1. Copies the production database file (or `pg_dump`s for Postgres)
   into `<state_root>/rehearsal/<id>.db`.
2. Runs `alembic upgrade head` against the copy via the same
   `_run_migrations` helper the server uses.
3. Reads the post-rehearsal revision and confirms it matches the
   candidate head.
4. Takes a final consistent backup immediately before cutover and
   records its path and `sha256`.

When the candidate head equals the live revision (no schema change),
the rehearsal phase is recorded as **inspected-not-required**, not
silently skipped.

## Promotion

The controller delegates the **exact** cutover to
`scripts/promote_release.sh <target_sha>` (no `--build-only`, no
`--no-promote`). The build phase was already completed and verified
during the rehearsal phase; promotion is just the symlink switch +
systemd drop-in rewrite + daemon-reload + restart + health probes
that the script already implements. The controller **never**
re-implements these steps in Python.

## Health-gated promotion

The controller re-checks before invoking the cutover script:

* the request still owns the update lease;
* maintenance mode is active;
* the live SHA still equals `expected_current_sha`;
* the candidate release still passes integrity verification (the
  release directory and manifest are still present and consistent);
* a database backup exists at the recorded path or the rehearsal
  record declares "no migration required".

After cutover the controller verifies:

* `systemctl is-active` reports both `omnigent-eval-web.service` and
  `omnigent-eval-host.service` active;
* the host daemon's running executable lives under
  ``<deploy_root>/releases/<target_sha>/.venv`` — the host daemon
  cannot run from the mutable repository checkout or from a
  different release (a readlink of ``/proc/<MainPID>/exe`` is
  compared against the target release's ``.venv`` prefix);
* `/health` on the live port returns a non-empty body;
* the `/` route returns the full web shell, not the
  `OMNIGENT_SKIP_WEB_UI` API-only landing page;
* the deployed SHA recorded at `~/.omnigent/deployed-sha` matches
  `target_sha`;
* a freshly spawned runner resolves the candidate release's
  `.venv/bin/python` from the target release directory.

Only after every check passes does the controller record
`succeeded`.

## Rollback

Any failure after cutover begins transitions to
`rolling_back → rolled_back`. The controller invokes
`scripts/rollback_release.sh` (the existing, hardened rollback
script) rather than implementing its own rollback path. After
rollback, the controller re-runs the same health-gated verification
against the restored release:

* `/health`, `/`, deployed SHA, manifest SHA match `previous_sha`.

`rollback_performed` and `rollback_result` are recorded in the
result file. `rollback_failed` is a distinct, loud terminal state.

## Result delivery

The final result reaches the conversation that initiated the update
even though Omnigent restarted during the operation:

* the controller persists the result file before attempting
  delivery;
* the controller posts the result to the web service's
  `POST /api/updater/result-deliver` endpoint;
* the endpoint inserts a comment with `path =
  "__system__/update-result.json"` so the conversation can detect
  it; the comment body is the structured result payload;
* a durable idempotency key (`request_id`) prevents duplicate
  deliveries — the endpoint refuses to insert a second comment for
  the same request id;
* if the web service is unreachable, the controller writes the
  result to a pending-deliveries file; the web service reconciles
  pending deliveries on every startup.

## Crash recovery

On startup the controller scans non-terminal requests and reconciles
them safely:

* `still queued` → no action; the controller resumes normal
  processing.
* `actively owned by another updater instance` → no action.
* `safe to resume` → resume from the next checkpoint phase.
* `already promoted but not verified` → re-run the verification
  phase and proceed.
* `already rolled back but not recorded` → record the rollback
  result.
* `complete but not yet delivered` → re-attempt delivery.
* `inconsistent` → mark the request for operator intervention.

Checkpoints are written **before and after** every externally
visible action so a crash never silently loses progress.

## Privileges

The updater has only the privileges it needs:

* read access to the upstream git checkout (`/home/hermes/workspace/repos/omnigent-eval`);
* write access to the deploy root and its state root;
* write access to the systemd drop-in directory and the deploy
  metadata files;
* the ability to invoke `systemctl restart` for the two Omnigent
  services (via a tightly scoped sudoers rule — no `NOPASSWD: ALL`,
  only the two named services plus `daemon-reload`);
* read access to the live database file (for backup / inspection);
* no shell access beyond invoking the named scripts.

## Tests

* `tests/updater/` — focused unit and integration tests:
  * request serialization and validation;
  * atomic request creation;
  * duplicate request handling;
  * state-machine validity;
  * single-active-updater locking;
  * stale expected-current SHA rejection;
  * malformed SHA rejection;
  * unknown target rejection;
  * out-of-lineage target rejection;
  * maintenance mode;
  * active-session drain;
  * crash recovery;
  * migration rehearsal;
  * result persistence;
  * exactly-once notification;
  * successful rollback;
  * rollback failure reporting;
  * updater restart survival.
* `tests/updater_acceptance/` — isolated staging acceptance tests
  that prove end-to-end behavior against a separate deployment root,
  state root, database copy, ports, and release symlinks.

The acceptance tests **never** touch the production deploy root.
