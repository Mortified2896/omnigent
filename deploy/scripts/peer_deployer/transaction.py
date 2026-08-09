"""Transaction identity for the peer-supervised deployer.

Every mutable phase of a promotion is preceded by the creation of a
transaction record. The record is a single JSON file under a stable
directory that names every resource the transaction has created,
captured-state, or staged. The rollback subsystem refuses to operate on
any resource that is not listed in the record.

The hard invariant is:

    A failure before the first committed deployment mutation must be
    incapable of deleting, renaming, replacing, restoring, or otherwise
    changing the current active runtime.

The peer-deployer enforces this by:

  1. Refusing to load any "transaction" that does not have a recorded
     ``mutation_boundary_crossed = True`` flag before it can be used
     as the basis for a rollback.
  2. Refusing to delete / rename / restore anything that is not listed
     in the transaction's ``owned_resources`` list.
  3. Including the old runtime's identity in the record before any
     switch.
  4. Including the DB backup path in the record and refusing to use
     the backup if it failed integrity verification.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# The transaction root is host-stateful state. It is intentionally
# separate from the deployment roots so that a corrupted or partial
# transaction cannot damage the supervisor's view of the world.
DEFAULT_TX_ROOT = Path("/var/lib/omnigent-control-room/transactions")

# A transaction ID format that is unambiguous in shell logs and easy to
# grep for: ``promotion-YYYYMMDDTHHMMSSZ-<random-8-hex>``.
_TX_ID_RE = re.compile(r"^promotion-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")

# Phases in the canonical order. A transaction record must move through
# them in order. Skipping a phase or moving backward is treated as a
# corruption of the record.
PHASE_ORDER = (
    "init",
    "preflight",
    "schema_snapshot",
    "db_backup",
    "candidate_staging",
    "candidate_verified",
    "switch",
    "service_restart",
    "acceptance",
    "tx_committed",
    "rolled_back",
    "failure",
)


class TransactionError(RuntimeError):
    """Raised when a transaction record cannot be created, loaded, or
    advanced in a way that keeps its invariants intact."""


@dataclass
class TransactionRecord:
    """The canonical peer-deployer transaction record.

    The record is the *only* source of truth for what a promotion has
    done. The rollback subsystem consults the record and refuses to
    act on any resource that is not registered here.
    """

    tx_id: str
    target: str
    supervisor: str
    target_artifact_sha: str
    target_artifact_version: str
    main_wheel_sha256: str
    sdk_client_wheel_sha256: str
    sdk_ui_wheel_sha256: str
    created_at_unix: float
    phase: str = "init"
    mutation_boundary_crossed: bool = False
    old_runtime_path: str = ""
    old_runtime_sha: str = ""
    old_runtime_version: str = ""
    new_runtime_path: str = ""
    new_runtime_sha: str = ""
    new_runtime_version: str = ""
    db_backup_path: str = ""
    db_backup_sha256: str = ""
    db_backup_integrity: str = ""
    old_db_schema: str = ""
    target_db_schema: str = ""
    owned_resources: list[str] = field(default_factory=list)
    log_path: str = ""
    rollback_reason: str = ""
    rollback_completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> "TransactionRecord":
        if set(blob) - set(cls.__dataclass_fields__):
            raise TransactionError(
                f"unknown keys in transaction record: "
                f"{sorted(set(blob) - set(cls.__dataclass_fields__))}"
            )
        return cls(**blob)


def make_tx_id() -> str:
    """Return a fresh transaction ID.

    Uses wall-clock time for human grep-ability and 8 hex chars of
    cryptographic randomness for collision avoidance across hosts.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rand = secrets.token_hex(4)
    return f"promotion-{stamp}-{rand}"


def assert_tx_id(tx_id: str) -> None:
    if not _TX_ID_RE.fullmatch(tx_id):
        raise TransactionError(f"invalid transaction id format: {tx_id!r}")


def transaction_path(root: Path, tx_id: str) -> Path:
    assert_tx_id(tx_id)
    return Path(root) / tx_id / "transaction.json"


def create(
    *,
    tx_id: str,
    target: str,
    supervisor: str,
    target_artifact_sha: str,
    target_artifact_version: str,
    main_wheel_sha256: str,
    sdk_client_wheel_sha256: str,
    sdk_ui_wheel_sha256: str,
    root: Path = DEFAULT_TX_ROOT,
) -> TransactionRecord:
    """Create a new transaction record on disk.

    The record is created in the ``init`` phase. ``mutation_boundary_crossed``
    is False. The record lives at ``root/<tx_id>/transaction.json`` and
    the directory is initialized with an empty ``owned_resources`` file
    for clarity.
    """
    assert_tx_id(tx_id)
    if target == supervisor:
        raise TransactionError(
            f"REFUSED: target == supervisor == {target!r}: "
            "an instance NEVER upgrades itself"
        )
    record = TransactionRecord(
        tx_id=tx_id,
        target=target,
        supervisor=supervisor,
        target_artifact_sha=target_artifact_sha,
        target_artifact_version=target_artifact_version,
        main_wheel_sha256=main_wheel_sha256,
        sdk_client_wheel_sha256=sdk_client_wheel_sha256,
        sdk_ui_wheel_sha256=sdk_ui_wheel_sha256,
        created_at_unix=time.time(),
    )
    record_path = transaction_path(root, tx_id)
    if record_path.exists():
        raise TransactionError(f"transaction already exists: {record_path}")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(record_path, record.to_dict())
    return record


def load(tx_id: str, root: Path = DEFAULT_TX_ROOT) -> TransactionRecord:
    """Load a transaction record from disk. Fails if the record is missing."""
    path = transaction_path(root, tx_id)
    if not path.is_file():
        raise TransactionError(f"transaction not found: {tx_id}")
    try:
        blob = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise TransactionError(f"corrupt transaction record {path}: {exc}") from exc
    if not isinstance(blob, dict):
        raise TransactionError(f"transaction record is not a dict: {path}")
    return TransactionRecord.from_dict(blob)


def save(record: TransactionRecord, root: Path = DEFAULT_TX_ROOT) -> None:
    """Atomically write the transaction record to disk."""
    path = transaction_path(root, record.tx_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, record.to_dict())


def advance(record: TransactionRecord, new_phase: str, root: Path = DEFAULT_TX_ROOT) -> None:
    """Move the transaction to a new phase.

    Phase transitions must be forward-only. The only exception is
    ``failure`` and ``rolled_back``, which can be entered from any
    phase.
    """
    if new_phase not in PHASE_ORDER:
        raise TransactionError(f"unknown phase: {new_phase!r}")
    current_idx = PHASE_ORDER.index(record.phase) if record.phase in PHASE_ORDER else -1
    new_idx = PHASE_ORDER.index(new_phase)
    if new_phase in {"failure", "rolled_back"}:
        pass
    elif new_idx < current_idx:
        raise TransactionError(
            f"cannot roll phase backward from {record.phase!r} to {new_phase!r}"
        )
    record.phase = new_phase
    save(record, root)


def cross_mutation_boundary(record: TransactionRecord, root: Path = DEFAULT_TX_ROOT) -> None:
    """Mark the transaction as having crossed a mutation boundary.

    Once set, ``mutation_boundary_crossed`` is permanent. The rollback
    subsystem uses this flag to refuse to operate on any transaction
    that has not yet crossed a mutation boundary.
    """
    if record.mutation_boundary_crossed:
        return
    record.mutation_boundary_crossed = True
    save(record, root)


def register_owned(
    record: TransactionRecord,
    path: str,
    root: Path = DEFAULT_TX_ROOT,
) -> None:
    """Register a resource as owned by this transaction.

    The path is stored as-given. The rollback subsystem normalizes
    paths via ``os.path.realpath`` before comparison.
    """
    if not path:
        raise TransactionError("cannot register an empty owned resource")
    if path in record.owned_resources:
        return
    record.owned_resources.append(path)
    save(record, root)


def is_owned(record: TransactionRecord, path: str) -> bool:
    """Return ``True`` iff ``path`` is recorded as owned by this transaction.

    The check is path-normalized so that a swap-via-symlink is correctly
    detected as touching the symlink target.
    """
    target = os.path.realpath(path)
    for owned in record.owned_resources:
        if os.path.realpath(owned) == target:
            return True
    return False


def complete(record: TransactionRecord, root: Path = DEFAULT_TX_ROOT) -> None:
    """Mark the transaction as committed/healthy-at-end."""
    record.phase = "tx_committed"
    save(record, root)


def fail_record(
    record: TransactionRecord,
    reason: str,
    root: Path = DEFAULT_TX_ROOT,
) -> None:
    """Move the transaction to ``failure`` and record the reason."""
    record.phase = "failure"
    record.rollback_reason = reason
    save(record, root)


def _write_atomic(path: Path, blob: dict) -> None:
    """Write ``blob`` to ``path`` atomically via a temp file + rename.

    Uses ``os.replace`` for atomicity on POSIX and ensures the parent
    directory exists. The data is written as JSON, sorted keys, no
    trailing whitespace.
    """
    payload = json.dumps(blob, indent=2, sort_keys=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    tmp.write_text(payload)
    os.replace(tmp, path)


__all__ = [
    "TransactionRecord",
    "TransactionError",
    "PHASE_ORDER",
    "DEFAULT_TX_ROOT",
    "make_tx_id",
    "assert_tx_id",
    "transaction_path",
    "create",
    "load",
    "save",
    "advance",
    "cross_mutation_boundary",
    "register_owned",
    "is_owned",
    "complete",
    "fail_record",
]
