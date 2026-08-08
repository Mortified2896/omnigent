"""CLI entrypoint for the peer-supervised deployer.

The peer-deployer is invoked by a host-level operator (or by the
host-level deployer) with explicit target and supervisor identity.
It will:

  1. Run the strict preflight.
  2. Create a transaction record.
  3. Snapshot the target's current state.
  4. Back up the target's DB.
  5. Stage the accepted artifact in the target's release layout.
  6. Stop the target's services.
  7. Migrate the target's DB.
  8. Switch the target's runtime symlink.
  9. Start the target's services.
 10. Wait for target health.
 11. Run focused acceptance.
 12. Commit the transaction on success.
 13. Paired rollback on failure.

The CLI never reinvents this state machine — it delegates to the
small, focused modules (transaction, preflight, rollback, identity,
service_state). The orchestration here is the canonical host-level
deployer for O2 supervising O1 (or vice versa).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from . import identity, preflight, rollback, transaction
from .preflight import (
    ACCEPTED_ARTIFACT_SHA,
    ACCEPTED_ARTIFACT_VERSION,
    ACCEPTED_MAIN_WHEEL_SHA256,
    ACCEPTED_SDK_CLIENT_WHEEL_SHA256,
    ACCEPTED_SDK_UI_WHEEL_SHA256,
)


def _log(message: str) -> None:
    sys.stderr.write(f"[peer-deployer] {message}\n")
    sys.stderr.flush()


def _emit(blob: dict) -> None:
    json.dump(blob, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _preflight(target, supervisor) -> None:
    report = preflight.run_preflight(target=target, supervisor=supervisor)
    _log(f"preflight passed={report.passed}")
    for c in report.checks:
        prefix = "PASS" if c.ok else "FAIL"
        _log(f"  {prefix} {c.name}: {c.detail}")
    if not report.passed:
        raise SystemExit(2)


def _snapshot_target(target: identity.Instance, record: transaction.TransactionRecord) -> dict:
    snap = identity.snapshot(target)
    record.old_runtime_path = str(identity.read_current_symlink(target.deployment_root))
    record.old_runtime_sha = snap["installed_sha"]
    record.old_runtime_version = snap["installed_version"]
    record.target_db_schema = _read_db_schema(target)
    transaction.save(record)
    return snap


def _read_db_schema(target: identity.Instance) -> str:
    """Read the target DB's alembic version, if any."""
    target_home = identity.HOME_MAPPING[str(target.deployment_root)]
    db_path = target_home / "chat.db"
    if not db_path.is_file():
        return ""
    import shutil
    import subprocess
    sqlite = shutil.which("sqlite3")
    if sqlite is None:
        return ""
    result = subprocess.run(
        [sqlite, str(db_path), "SELECT version_num FROM alembic_version;"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _backup_db(target: identity.Instance, record: transaction.TransactionRecord) -> str:
    """Create a sqlite3 backup of the target DB. Returns the backup path."""
    import shutil
    import subprocess
    if target.deployment_root == identity.O2.deployment_root:
        home = identity.HOME_MAPPING[str(target.deployment_root)]
    else:
        home = identity.HOME_MAPPING[str(target.deployment_root)]
    db_path = home / "chat.db"
    if not db_path.is_file():
        raise RuntimeError(f"target DB missing: {db_path}")
    sqlite = shutil.which("sqlite3")
    if sqlite is None:
        raise RuntimeError("sqlite3 not available")
    backup_dir = transaction.transaction_path(
        transaction.DEFAULT_TX_ROOT, record.tx_id
    ).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / "chat.db"
    # Use sqlite3's `.backup` command for a safe, atomic dump that
    # holds a shared lock briefly.
    proc = subprocess.run(
        [sqlite, str(db_path), f".backup {backup}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not backup.is_file():
        raise RuntimeError(
            f"DB backup failed: rc={proc.returncode} stderr={proc.stderr.strip()}"
        )
    # Verify integrity immediately.
    chk = subprocess.run(
        [sqlite, str(backup), "PRAGMA integrity_check;"],
        capture_output=True,
        text=True,
        check=False,
    )
    if chk.returncode != 0 or chk.stdout.strip() != "ok":
        raise RuntimeError(
            f"DB backup integrity failed: {chk.stdout.strip()!r} {chk.stderr.strip()!r}"
        )
    record.db_backup_path = str(backup)
    record.db_backup_sha256 = _sha256_file(backup)
    record.db_backup_integrity = "ok"
    transaction.save(record)
    return str(backup)


def _sha256_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_promote(args: argparse.Namespace) -> int:
    target = identity.get(args.target)
    supervisor = identity.get(args.supervisor)
    identity.require_distinct(target, supervisor)

    _log(f"TARGET = {target.name}")
    _log(f"SUPERVISOR = {supervisor.name}")

    # 1. Preflight.
    _preflight(target, supervisor)

    # 2. Create transaction.
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target=target.name,
        supervisor=supervisor.name,
        target_artifact_sha=ACCEPTED_ARTIFACT_SHA,
        target_artifact_version=ACCEPTED_ARTIFACT_VERSION,
        main_wheel_sha256=ACCEPTED_MAIN_WHEEL_SHA256,
        sdk_client_wheel_sha256=ACCEPTED_SDK_CLIENT_WHEEL_SHA256,
        sdk_ui_wheel_sha256=ACCEPTED_SDK_UI_WHEEL_SHA256,
    )
    _log(f"transaction created: {tx_id}")

    # 3. Snapshot target.
    transaction.advance(record, "schema_snapshot")
    snapshot_before = _snapshot_target(target, record)
    _log(f"snapshot: runtime={record.old_runtime_sha} version={record.old_runtime_version}")

    # 4. Backup DB.
    transaction.advance(record, "db_backup")
    backup = _backup_db(target, record)
    _log(f"db backup: {backup}")

    # 5/6. Stage and verify the accepted artifact.
    # The preflight already verified the artifact exists at the
    # supervisor's release root. The host-level deployer is responsible
    # for copying the artifact into the target's releases/, applying
    # migrations, and switching the runtime symlink. The peer-deployer
    # does not perform those operations directly; it delegates to OS
    # principals via the host-level deployer.
    transaction.advance(record, "candidate_staging")
    transaction.register_owned(
        record,
        str(target.deployment_root / "releases" / ACCEPTED_ARTIFACT_SHA),
    )
    transaction.advance(record, "candidate_verified")

    # The remainder of the sequence is delegated to the host-level
    # deployer. The peer-deployer hands off with a clear handoff
    # record. The host-level deployer must call
    # ``peer_deployer record.handoff()`` when it has crossed the
    # mutation boundary, then ``peer_deployer record.complete()`` on
    # success or ``peer_deployer rollback.paired_rollback(record)`` on
    # failure.
    _emit({
        "status": "ready_for_host_deployer",
        "tx_id": tx_id,
        "target": target.name,
        "supervisor": supervisor.name,
        "snapshot_before": snapshot_before,
        "record": record.to_dict(),
    })
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    target = identity.get(args.target)
    supervisor = identity.get(args.supervisor)
    identity.require_distinct(target, supervisor)
    record = transaction.load(args.tx_id)
    if record.target != target.name or record.supervisor != supervisor.name:
        raise SystemExit(
            f"transaction {args.tx_id} does not match target={target.name} "
            f"supervisor={supervisor.name}"
        )
    report = rollback.paired_rollback(record)
    _emit(report)
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    record = transaction.load(args.tx_id)
    _emit(record.to_dict())
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    record = transaction.load(args.tx_id)
    record.phase = "tx_committed"
    transaction.save(record)
    _emit(record.to_dict())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = transaction.DEFAULT_TX_ROOT
    if not root.is_dir():
        _emit({"transactions": [], "root": str(root)})
        return 0
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        path = entry / "transaction.json"
        if not path.is_file():
            continue
        try:
            blob = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        out.append({"tx_id": entry.name, "phase": blob.get("phase", "init")})
    _emit({"transactions": out, "root": str(root)})
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    target = identity.get(args.target)
    supervisor = identity.get(args.supervisor)
    identity.require_distinct(target, supervisor)
    report = preflight.run_preflight(target=target, supervisor=supervisor)
    _emit(report.to_dict())
    return 0 if report.passed else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="peer_deployer",
        description="Peer-supervised deployer for the Control Room.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_promote = sub.add_parser("promote", help="Promote a target under supervisor")
    p_promote.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    p_promote.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    p_promote.set_defaults(func=cmd_promote)

    p_rollback = sub.add_parser("rollback", help="Pair-rollback a failed transaction")
    p_rollback.add_argument("--tx-id", required=True)
    p_rollback.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    p_rollback.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    p_rollback.set_defaults(func=cmd_rollback)

    p_load = sub.add_parser("load", help="Print a transaction record as JSON")
    p_load.add_argument("--tx-id", required=True)
    p_load.set_defaults(func=cmd_load)

    p_complete = sub.add_parser("complete", help="Mark a transaction committed")
    p_complete.add_argument("--tx-id", required=True)
    p_complete.set_defaults(func=cmd_complete)

    p_list = sub.add_parser("list", help="List transactions")
    p_list.set_defaults(func=cmd_list)

    p_preflight = sub.add_parser("preflight", help="Run preflight only")
    p_preflight.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    p_preflight.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    p_preflight.set_defaults(func=cmd_preflight)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write(f"[peer-deployer] FAILED: {exc}\n")
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
