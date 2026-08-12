"""Failure-state machine for the peer-deployed promotion.

The brief requires an explicit failure-state machine that the
rollback subsystem consults to choose a safe disposition. The
state machine is deterministic and tied to the durable
transaction record; the rollback subsystem never infers state
from the absence of files.

States (after the standard transaction phase names):

  * PREFLIGHT           — gate checks; no mutation yet
  * STAGED              — candidate venv is verified under
                          /opt/omnigent/staging/<TX_ID>;
                          .complete marker present
  * RUNTIME_BACKED_UP   — old runtime renamed to
                          /opt/omnigent/venv.legacy-<TX_ID>
  * SWITCHED            — venv symlink points to the new release
  * DB_MIGRATED         — DB schema is at the new target schema
  * SERVICES_STARTED    — services restarted and health-ok
  * ACCEPTED            — focused acceptance passed; transaction
                          is tx_committed and the candidate is the
                          active runtime

Between adjacent states, the rollback subsystem MUST be able to
recover deterministically. The rules are:

  * Before MUTATION_BOUNDARY (preflight, db_backup, staged):
    Failure may ONLY clean transaction-created staging
    resources. NO /opt/omnigent/venv touch. NO DB touch. NO
    service stops. NO rollback. NO paired restore.

  * After RUNTIME_BACKED_UP but before SWITCHED:
    The pre-recorded old runtime path is the rollback target.
    Restore the old runtime path; no DB touch yet.

  * After SWITCHED but before DB_MIGRATED:
    The symlink points to the new candidate. Rollback swaps
    the symlink back to the old runtime. NO DB touch.

  * After DB_MIGRATED:
    Rollback pairs:
      - previous runtime (the renamed /opt/omnigent/venv.legacy-<TX_ID>)
      - previous DB (the verified backup)
    Both must land before the transaction is marked rolled_back.

  * After SERVICES_STARTED:
    Same as DB_MIGRATED plus accept dance.

  * Unknown/incomplete metadata:
    REFUSE. Never infer.

The rollback_state_for(record) function maps the durable
transaction phase to a (state_name, rollback_disposition)
pair. The rollback subsystem uses this map to choose the
correct recovery path.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from . import transaction
from .transaction import TransactionRecord


class State(str, Enum):
    """The canonical promotion states."""

    PREFLIGHT = "preflight"
    STAGED = "staged"
    RUNTIME_BACKED_UP = "runtime_backed_up"
    SWITCHED = "switched"
    DB_MIGRATED = "db_migrated"
    SERVICES_STARTED = "services_started"
    ACCEPTED = "accepted"


# Map the transaction record's phase to the corresponding State.
# The phase-to-state mapping is the durable source of truth;
# nothing on the filesystem is consulted.
PHASE_TO_STATE: dict[str, State] = {
    "init": State.PREFLIGHT,
    "preflight": State.PREFLIGHT,
    "schema_snapshot": State.PREFLIGHT,
    "db_backup": State.PREFLIGHT,
    "candidate_staging": State.STAGED,
    "candidate_verified": State.STAGED,
    "switch": State.SWITCHED,
    "service_restart": State.SERVICES_STARTED,
    "acceptance": State.SERVICES_STARTED,
    "tx_committed": State.ACCEPTED,
    "rolled_back": State.ACCEPTED,  # terminal rolled-back state
    "failure": State.ACCEPTED,  # terminal post-failure state
}


@dataclass(frozen=True)
class Disposition:
    """The rollback disposition for a given state.

    The fields encode "what is allowed" and "what is forbidden"
    for the rollback subsystem. The subsystem consults these
    rather than the filesystem, so the disposition is
    deterministic and reproducible from a separate process.
    """

    state: State
    can_touch_active_runtime: bool
    can_touch_db: bool
    can_stop_services: bool
    can_clear_transaction_staging: bool
    must_verify_db_backup: bool
    must_verify_old_runtime: bool
    notes: tuple[str, ...] = ()


# The canonical disposition table. These are the only allowed
# dispositions; any new state must be added here explicitly.
DISPOSITIONS: dict[State, Disposition] = {
    State.PREFLIGHT: Disposition(
        state=State.PREFLIGHT,
        can_touch_active_runtime=False,
        can_touch_db=False,
        can_stop_services=False,
        can_clear_transaction_staging=True,
        must_verify_db_backup=False,
        must_verify_old_runtime=False,
        notes=(
            "Failure before mutation: clean only transaction-created "
            "staging resources. Do not touch the active runtime, "
            "the DB, or the services."
        ),
    ),
    State.STAGED: Disposition(
        state=State.STAGED,
        can_touch_active_runtime=False,
        can_touch_db=False,
        can_stop_services=False,
        can_clear_transaction_staging=True,
        must_verify_db_backup=False,
        must_verify_old_runtime=False,
        notes=(
            "Candidate is verified but live runtime is untouched. "
            "Clean only the transaction's staging dir."
        ),
    ),
    State.RUNTIME_BACKED_UP: Disposition(
        state=State.RUNTIME_BACKED_UP,
        can_touch_active_runtime=True,  # only the renamed legacy path
        can_touch_db=False,
        can_stop_services=False,
        can_clear_transaction_staging=False,
        must_verify_db_backup=False,
        must_verify_old_runtime=True,
        notes=(
            "Old runtime has been renamed to venv.legacy-<TX_ID>. "
            "Rollback restores the symlink to that legacy path. "
            "DB is untouched."
        ),
    ),
    State.SWITCHED: Disposition(
        state=State.SWITCHED,
        can_touch_active_runtime=True,
        can_touch_db=False,
        can_stop_services=False,
        can_clear_transaction_staging=False,
        must_verify_db_backup=False,
        must_verify_old_runtime=True,
        notes=(
            "Symlink points to the new candidate. Rollback swaps "
            "the symlink back to the legacy path. DB is untouched."
        ),
    ),
    State.DB_MIGRATED: Disposition(
        state=State.DB_MIGRATED,
        can_touch_active_runtime=True,
        can_touch_db=True,
        can_stop_services=False,
        can_clear_transaction_staging=False,
        must_verify_db_backup=True,
        must_verify_old_runtime=True,
        notes=(
            "DB is at the new schema. Rollback pairs runtime "
            "restore with DB restore from the verified backup. "
            "Both must succeed before the transaction is marked "
            "rolled_back."
        ),
    ),
    State.SERVICES_STARTED: Disposition(
        state=State.SERVICES_STARTED,
        can_touch_active_runtime=True,
        can_touch_db=True,
        can_stop_services=True,
        can_clear_transaction_staging=False,
        must_verify_db_backup=True,
        must_verify_old_runtime=True,
        notes=(
            "Services restarted. Rollback stops services, restores "
            "runtime, restores DB, restarts services, then verifies."
        ),
    ),
    State.ACCEPTED: Disposition(
        state=State.ACCEPTED,
        can_touch_active_runtime=False,
        can_touch_db=False,
        can_stop_services=False,
        can_clear_transaction_staging=False,
        must_verify_db_backup=False,
        must_verify_old_runtime=False,
        notes=(
            "Terminal state. No rollback is permitted. The "
            "transaction is either committed (tx_committed) or "
            "already classified (rolled_back / failure)."
        ),
    ),
}


def disposition_for(record: TransactionRecord) -> Disposition:
    """Return the rollback disposition for the transaction's current phase.

    Refuses to operate on a transaction whose phase is unknown
    or whose metadata is incomplete.
    """
    if record.phase not in PHASE_TO_STATE:
        raise transaction.TransactionError(
            f"REFUSED: transaction phase {record.phase!r} is unknown; "
            "rollback disposition cannot be inferred"
        )
    state = PHASE_TO_STATE[record.phase]
    disposition = DISPOSITIONS[state]

    # Integrity checks: the disposition imposes minimum
    # requirements on the transaction record.
    if disposition.must_verify_db_backup:
        if not record.db_backup_path:
            raise transaction.TransactionError(
                f"REFUSED: state {state.value} requires db_backup_path; "
                "transaction record is incomplete"
            )
        if record.db_backup_integrity != "ok":
            raise transaction.TransactionError(
                f"REFUSED: state {state.value} requires db_backup_integrity "
                f"to be 'ok'; got {record.db_backup_integrity!r}"
            )
    if disposition.must_verify_old_runtime:
        if not record.old_runtime_path:
            raise transaction.TransactionError(
                f"REFUSED: state {state.value} requires old_runtime_path; "
                "transaction record is incomplete"
            )
    return disposition


def verify_old_runtime_path(path: str) -> Path:
    """Return the resolved old runtime path or raise.

    The path is the value stored in the transaction record. It
    must resolve to a tx-specific legacy path under the O1
    deployment root. The check is path-normalized and refuses
    paths that do not match the tx-specific legacy layout.
    """
    if not path:
        raise transaction.TransactionError("old_runtime_path is empty")
    p = Path(path)
    if not p.is_absolute():
        raise transaction.TransactionError(
            f"REFUSED: old_runtime_path is not absolute: {path!r}"
        )
    if "venv.legacy-" not in p.name:
        raise transaction.TransactionError(
            f"REFUSED: old_runtime_path {path!r} is not a "
            "tx-specific legacy path (venv.legacy-<TX_ID>)"
        )
    return p.resolve()


def classify_phase(phase: str) -> Optional[State]:
    """Return the State corresponding to ``phase`` or None if unknown."""
    return PHASE_TO_STATE.get(phase)


__all__ = [
    "State",
    "PHASE_TO_STATE",
    "Disposition",
    "DISPOSITIONS",
    "disposition_for",
    "verify_old_runtime_path",
    "classify_phase",
]
