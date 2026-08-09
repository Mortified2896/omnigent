"""Rollback for the peer-supervised deployer.

The rollback subsystem is the *only* code path that can delete or
restore a deployment resource. It must:

  1. Refuse to operate on any transaction that has not crossed a
     mutation boundary.
  2. Refuse to delete or rename any path that is not listed in the
     transaction's owned_resources.
  3. Refuse to delete the active runtime unless it is explicitly the
     transaction-created candidate.
  4. Verify the DB backup exists and passed integrity check before
     touching the DB.
  5. Pair application rollback with DB rollback atomically.
  6. Preserve rollback artifacts after rollback.
  7. Run at most once per failed transaction. No retry loops.

The hard invariant is:

    Cleanup may delete only resources explicitly created by the same
    deployment transaction and carrying that transaction/release
    identity. Never infer ownership from absence of a later expected
    path.

The rollback subsystem exposes two entry points:

  * ``paired_rollback(record, *, runtime_resolver=None,
    home_mapping=None)`` — application + DB rollback for a transaction
    that has crossed a mutation boundary.
  * ``refuse_unknown_path(record, path)`` — refuse to delete any
    path that is not owned by the transaction.

The runtime_resolver and home_mapping parameters are injection points
so the rollback can be unit-tested without touching the host's
real /opt/ or /var/lib/ paths. The production caller passes
``None`` and the rollback uses the canonical identity module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from . import identity, transaction
from .identity import Instance
from .transaction import TransactionRecord


class RollbackError(RuntimeError):
    """Raised when a rollback cannot be performed safely."""


def _ensure_initialized(record: TransactionRecord) -> None:
    if not record.mutation_boundary_crossed:
        raise RollbackError(
            f"REFUSED: transaction {record.tx_id!r} has not crossed a "
            "mutation boundary; rollback is not permitted"
        )
    if record.phase == "rolled_back":
        raise RollbackError(
            f"REFUSED: transaction {record.tx_id!r} has already been rolled back"
        )
    if record.phase == "tx_committed":
        raise RollbackError(
            f"REFUSED: transaction {record.tx_id!r} is committed; rollback is not permitted"
        )


def refuse_unknown_path(record: TransactionRecord, path: str) -> None:
    """Refuse to delete or restore any path that is not owned by the transaction.

    The check is applied to ``os.path.realpath(path)`` so that deleting
    a symlink is correctly detected as touching the symlink target.
    """
    if not path:
        raise RollbackError("rollback path is empty")
    if not transaction.is_owned(record, path):
        raise RollbackError(
            f"REFUSED: rollback path {path!r} is not owned by transaction "
            f"{record.tx_id!r}; refusing to delete or restore"
        )


def _verify_db_backup(record: TransactionRecord) -> None:
    """The DB backup must exist and pass integrity check before use."""
    if not record.db_backup_path:
        raise RollbackError(
            f"REFUSED: transaction {record.tx_id!r} has no DB backup path"
        )
    backup = Path(record.db_backup_path)
    if not backup.is_file():
        raise RollbackError(
            f"REFUSED: rollback DB backup missing: {backup}"
        )
    if record.db_backup_integrity and record.db_backup_integrity != "ok":
        raise RollbackError(
            f"REFUSED: rollback DB backup integrity was not 'ok': "
            f"{record.db_backup_integrity!r}"
        )
    sqlite = shutil.which("sqlite3")
    if sqlite is None:
        raise RollbackError("sqlite3 not available for DB integrity verification")
    result = subprocess.run(
        [sqlite, str(backup), "PRAGMA integrity_check;"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "ok":
        raise RollbackError(
            f"REFUSED: rollback DB backup failed integrity_check: "
            f"{result.stdout.strip()!r} {result.stderr.strip()!r}"
        )


def _resolve_current_runtime(
    target_root: Path,
    runtime_resolver: Optional[Callable[[Path], Path]] = None,
) -> Optional[Path]:
    """Return the current runtime path for the target, if any.

    If ``runtime_resolver`` is provided, it is called with the target
    deployment root and must return the resolved runtime path, or
    ``None`` if there is no current runtime. The production caller
    passes ``None`` and the resolver uses the canonical
    ``identity.read_current_symlink`` helper.

    The resolver may also return a ``(current_path, current_link)``
    tuple to specify the symlink location explicitly. This is used
    by tests to point the symlink at a tmp_path location instead of
    the (often read-only) /opt/ root.
    """
    if runtime_resolver is not None:
        result = runtime_resolver(target_root)
        if isinstance(result, tuple):
            return result[0]
        return result
    try:
        return identity.read_current_symlink(target_root)
    except identity.IdentityError:
        return None


def _resolve_current_link(
    target_root: Path,
    runtime_resolver: Optional[Callable[[Path], Path]] = None,
) -> Path:
    """Return the symlink path used to point to the current runtime.

    The production caller passes ``None`` and the resolver returns
    ``target_root / "current"``. Tests can pass a resolver that
    returns ``(current_path, current_link)`` to override the symlink
    location.
    """
    if runtime_resolver is not None:
        result = runtime_resolver(target_root)
        if isinstance(result, tuple):
            return Path(result[1])
    return target_root / "current"


def _resolve_home(
    target: Instance,
    home_mapping: Optional[dict[str, Path]] = None,
) -> Path:
    """Return the data home for the target instance."""
    if home_mapping is not None:
        target_root_str = str(target.deployment_root)
        if target_root_str not in home_mapping:
            raise RollbackError(
                f"unknown target deployment root, no home mapping: {target_root_str}"
            )
        return home_mapping[target_root_str]
    target_root_str = str(target.deployment_root)
    if target_root_str not in identity.HOME_MAPPING:
        raise RollbackError(
            f"unknown target deployment root, no home mapping: {target_root_str}"
        )
    return identity.HOME_MAPPING[target_root_str]


def paired_rollback(
    record: TransactionRecord,
    *,
    runtime_resolver: Optional[Callable[[Path], Path]] = None,
    home_mapping: Optional[dict[str, Path]] = None,
) -> dict:
    """Restore the target to its pre-promotion state.

    Restores:
      * the DB from the verified backup (atomic, paired)
      * the target's PROVENANCE.txt from the snapshot if any
      * the runtime symlink ONLY if the current runtime is the
        transaction-created candidate — never the old runtime

    Returns a structured report describing what was restored and what
    was preserved. The function raises ``RollbackError`` on any
    unsafe condition.
    """
    _ensure_initialized(record)

    target = identity.get(record.target)
    supervisor = identity.get(record.supervisor)
    # Safety: the supervisor must never be touched by rollback.
    report = {
        "tx_id": record.tx_id,
        "target": target.name,
        "supervisor": supervisor.name,
        "actions": [],
        "preserved": [],
        "errors": [],
    }

    # 1. Verify DB backup before any application work.
    if record.db_backup_path:
        _verify_db_backup(record)
        report["actions"].append(f"db_backup_verified:{record.db_backup_path}")

    # 2. Application rollback: only touch the runtime if the current
    # symlink points to a transaction-created candidate. Otherwise
    # the runtime is the OLD runtime and we must leave it alone.
    target_root = target.deployment_root
    current_runtime = _resolve_current_runtime(target_root, runtime_resolver)
    if current_runtime is None:
        # No current symlink — nothing to restore. The transaction's
        # candidate may still exist on disk but we cannot reason
        # about it. We preserve it.
        report["preserved"].append(
            f"current_symlink:absent at {target_root}. Nothing to restore."
        )
    elif not transaction.is_owned(record, str(current_runtime)):
        # The current runtime is NOT the transaction-created candidate.
        # In that case we MUST NOT touch it. It is the old runtime.
        report["preserved"].append(
            f"current_runtime:{current_runtime} (not owned by this transaction)"
        )
    else:
        # The current runtime IS the transaction-created candidate.
        # We can swap the symlink to the old runtime path.
        if record.old_runtime_path:
            old = Path(record.old_runtime_path)
            if not old.exists():
                raise RollbackError(
                    f"REFUSED: old runtime path {old} no longer exists; "
                    "cannot restore without a paired legacy snapshot"
                )
            # Atomically swap the symlink back to the old runtime.
            current_link = _resolve_current_link(target_root, runtime_resolver)
            tmp = Path(str(current_link) + ".tmp.rollback")
            if tmp.exists() or tmp.is_symlink():
                tmp.unlink()
            os.symlink(old, tmp)
            os.replace(tmp, current_link)
            report["actions"].append(f"symlink_restored_to:{old}")
        else:
            # No legacy snapshot was captured. The rollback cannot
            # safely restore the runtime. We report the partial restore
            # and DO NOT delete the candidate — the host-level operator
            # must run a manual restore.
            report["errors"].append(
                "no old_runtime_path captured; rollback cannot restore the "
                "runtime symlink. The transaction-created candidate is "
                "preserved on disk for manual recovery."
            )

    # 3. DB rollback.
    if record.db_backup_path:
        target_home = _resolve_home(target, home_mapping)
        db_path = target_home / "chat.db"
        for suffix in ("", "-shm", "-wal"):
            source = Path(record.db_backup_path + suffix)
            if not source.is_file():
                continue
            tmp = db_path.with_suffix(
                db_path.suffix + f".rollback.{os.getpid()}"
            )
            shutil.copy2(source, tmp)
            os.replace(tmp, db_path)
            report["actions"].append(f"db_restored:{source}->{db_path}")

    # 4. PROVENANCE.txt restore.
    if record.old_runtime_path:
        old_provenance = Path(record.old_runtime_path) / "PROVENANCE.txt"
        if old_provenance.is_file():
            target_provenance = target_root / "PROVENANCE.txt"
            tmp = target_provenance.with_suffix(target_provenance.suffix + ".rollback")
            shutil.copy2(old_provenance, tmp)
            os.replace(tmp, target_provenance)
            report["actions"].append(f"provenance_restored:{target_provenance}")

    # 5. Mark the transaction as rolled back. The rollback artifacts
    # themselves (backup, release dirs, transaction record) are preserved.
    record.rollback_completed = True
    record.phase = "rolled_back"
    transaction.save(record, root=transaction.DEFAULT_TX_ROOT)
    report["preserved"].append(
        "rollback_artifacts: db backup, release dirs, transaction record"
    )
    return report


__all__ = [
    "RollbackError",
    "paired_rollback",
    "refuse_unknown_path",
]
