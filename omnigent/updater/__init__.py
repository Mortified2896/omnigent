"""External, rollback-safe self-update controller for Omnigent.

Issue #38 — the controller lives outside the release being replaced,
survives Omnigent web-service restarts, and never imports its runtime
code from the live deployment. It owns:

* durable request, event, checkpoint, and result records (see
  :mod:`omnigent.updater.protocol` and :mod:`omnigent.updater.store`);
* strict target and lineage validation
  (:mod:`omnigent.updater.validation`);
* a single-active-update lock (:mod:`omnigent.updater.locking`);
* maintenance / drain integration with the web service
  (:mod:`omnigent.updater.maintenance`);
* migration rehearsal and backup orchestration
  (:mod:`omnigent.updater.migration_rehearsal`);
* health-gated promotion and rollback orchestration
  (:mod:`omnigent.updater.controller`);
* post-restart result reconciliation and exactly-once delivery to the
  originating conversation (:mod:`omnigent.updater.runner_client`).

The state root, deploy root, and repository root are configurable via
environment variables (see :mod:`omnigent.updater.layout`). Tests and
staging always set them to tmpdirs so production paths are never
touched.

:see: ``docs/deployments/omnigent-updater.md`` for the architecture
       overview and the operator runbook.
"""

from __future__ import annotations

from omnigent.updater.controller import UpdaterController
from omnigent.updater.protocol import (
    TERMINAL_STATES,
    Authorization,
    RequestRecord,
    ResultRecord,
)
from omnigent.updater.state_machine import (
    STATE_TRANSITIONS,
    UpdatePhase,
    is_terminal,
    validate_transition,
)
from omnigent.updater.store import UpdaterStore
from omnigent.updater.validation import (
    LineageRejectedError,
    MalformedShaError,
    StaleExpectedCurrentError,
    TargetMissingError,
    ValidationError,
    validate_request,
)

__all__ = [
    "STATE_TRANSITIONS",
    "TERMINAL_STATES",
    "Authorization",
    "LineageRejectedError",
    "MalformedShaError",
    "RequestRecord",
    "ResultRecord",
    "StaleExpectedCurrentError",
    "TargetMissingError",
    "UpdatePhase",
    "UpdaterController",
    "UpdaterStore",
    "ValidationError",
    "is_terminal",
    "validate_request",
    "validate_transition",
]
