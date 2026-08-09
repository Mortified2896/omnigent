"""CLI entrypoint for the peer-supervised deployer.

The CLI is the inspection / maintenance surface for the peer-deployer.
It exposes:

  * ``preflight``         — run the strict preflight gate
  * ``stage``             — deterministically stage a candidate
  * ``rollback``          — pair-rollback a failed transaction
  * ``complete``          — mark a transaction committed
  * ``load``              — print a transaction record as JSON
  * ``list``              — list all transaction records
  * ``reconcile-stale``   — first-class reconcile of a stale
                            transaction; quarantines the candidate
                            if it is independently proven safe.
                            Preserves the historical transaction
                            record byte-identical.

The CLI does NOT expose a generic ``promote`` subcommand. The
canonical live promotion entrypoint is
``deploy/scripts/peer_promote_o1_v3.py``. The CLI exists for
inspection, automated maintenance, and reconciliation, not as
an alternate deploy path.

The CLI never reinvents the state machine — it delegates to the
small, focused modules (transaction, preflight, rollback, identity,
path_safety, reconcile, fsm, service_state).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

from . import (
    fsm,
    identity,
    path_safety,
    preflight,
    reconcile,
    rollback,
    staging,
    transaction,
)
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


def cmd_promote(args: argparse.Namespace) -> int:
    """REFUSED: the CLI does not expose a generic promote subcommand.

    The canonical live promotion entrypoint is the host-level
    deployer at ``deploy/scripts/peer_promote_o1_v3.py``. That
    script enforces the brief's hard architectural invariants
    (TARGET=O1, SUPERVISOR=O2, exact accepted artifact, paired
    rollback, supervised by O2, never run from inside an Omnigent
    sandbox). The CLI is for inspection and reconciliation only.

    Operators who want to promote must run the host entrypoint
    directly:

        sudo -E deploy/scripts/peer_promote_o1_v3.py --preflight-only
        sudo -E deploy/scripts/peer_promote_o1_v3.py --promote
    """
    raise SystemExit(
        "REFUSED: peer_deployer CLI does not expose a promote subcommand. "
        "Use deploy/scripts/peer_promote_o1_v3.py as the canonical host "
        "entrypoint. The CLI exposes preflight, stage, rollback, complete, "
        "load, list, and reconcile-stale only."
    )


def cmd_reconcile_stale(args: argparse.Namespace) -> int:
    """First-class stale-transaction reconciliation.

    Inspects the historical transaction record and the current
    filesystem state. If the candidate path is independently
    proven safe (not active runtime, not O2, not referenced by
    any service, not in the intrinsic-forbidden list), it is
    MOVED into a per-transaction quarantine directory. The
    historical transaction record is NEVER rewritten; a new
    audit record is written alongside the quarantine dir.

    This is the operator replacement for the old manual
    ``python -c 'edit transaction.json' && rm -rf /opt/omnigent/releases/<sha>``
    procedure. That procedure is not acceptable.
    """
    target = identity.get(args.target)
    supervisor = identity.get(args.supervisor)
    identity.require_distinct(target, supervisor)
    try:
        report = reconcile.reconcile_stale_transaction(
            args.tx_id,
            quarantine_root=reconcile.DEFAULT_QUARANTINE_ROOT,
            tx_root=transaction.DEFAULT_TX_ROOT,
            allowed_target=target,
            allowed_supervisor=supervisor,
        )
    except reconcile.ReconciliationError as exc:
        _emit({
            "status": "refused",
            "tx_id": args.tx_id,
            "reason": str(exc),
        })
        return 2
    _emit(report.to_dict())
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


def cmd_stage(args: argparse.Namespace) -> int:
    """Run the staging phase for an existing transaction.

    Reads the transaction, captures the supervisor's closure, and
    stages a complete candidate runtime under the transaction's
    staging root. On success, registers the staging root as owned
    and advances the transaction to ``candidate_verified``. On any
    failure, the partial staging root is removed before the function
    returns, so the next attempt starts from a clean slate.

    This command is the host-level deployer's entry point for the
    deterministic staging phase. It performs no network access and
    never touches the supervisor's runtime or the target's active
    runtime.
    """
    target = identity.get(args.target)
    supervisor = identity.get(args.supervisor)
    identity.require_distinct(target, supervisor)
    record = transaction.load(args.tx_id)
    if record.target != target.name or record.supervisor != supervisor.name:
        raise SystemExit(
            f"transaction {args.tx_id} does not match target={target.name} "
            f"supervisor={supervisor.name}"
        )
    staging_root = staging.transaction_owned_staging_path(target, args.tx_id)
    if staging_root.exists() and (staging_root / ".complete").is_file():
        _log(f"staging root already complete at {staging_root}; "
             "running verification gate only")
    else:
        if staging_root.exists():
            _log(f"removing previous partial staging at {staging_root}")
            shutil.rmtree(staging_root)
        supervisor_release = (
            identity.O2.deployment_root
            / "releases"
            / ACCEPTED_ARTIFACT_SHA
        )
        if not supervisor_release.is_dir():
            raise SystemExit(
                f"supervisor release missing: {supervisor_release}"
            )
        artifacts = supervisor_release / "artifacts"
        wheels: dict[str, Path] = {}
        for pattern, label in (
            ("omnigent-*.whl", "main"),
            ("omnigent_client-*.whl", "sdk_client"),
            ("omnigent_ui_sdk-*.whl", "sdk_ui"),
        ):
            matches = sorted(artifacts.glob(pattern))
            if not matches:
                raise SystemExit(f"missing {label} wheel under {artifacts}")
            wheels[label] = matches[0]
        try:
            closure = staging.stage_candidate_runtime(
                staging_root, supervisor, wheels
            )
        except staging.StagingError as exc:
            transaction.fail_record(
                record,
                f"staging failed: {exc}",
                root=transaction.DEFAULT_TX_ROOT,
            )
            raise SystemExit(f"STAGING FAILED: {exc}")
        manifest_path = staging.write_staging_manifest(staging_root, closure)
        _log(f"wrote staging manifest: {manifest_path}")
        transaction.register_owned(record, str(staging_root))
        transaction.register_owned(record, str(manifest_path))
    failures = staging.verify_candidate_complete(staging_root)
    if failures:
        transaction.fail_record(
            record,
            f"candidate verification failed: {' ; '.join(failures)}",
            root=transaction.DEFAULT_TX_ROOT,
        )
        try:
            shutil.rmtree(staging_root)
        except OSError:
            pass
        raise SystemExit(f"VERIFICATION FAILED: {' ; '.join(failures)}")
    if not staging.candidate_identity_matches(
        staging_root,
        ACCEPTED_ARTIFACT_SHA,
        ACCEPTED_ARTIFACT_VERSION,
    ):
        transaction.fail_record(
            record,
            f"candidate identity mismatch: expected {ACCEPTED_ARTIFACT_SHA}/"
            f"{ACCEPTED_ARTIFACT_VERSION}",
            root=transaction.DEFAULT_TX_ROOT,
        )
        try:
            shutil.rmtree(staging_root)
        except OSError:
            pass
        raise SystemExit("VERIFICATION FAILED: candidate identity mismatch")
    try:
        candidate_python = staging._candidate_python(staging_root)
        closure = staging.capture_supervisor_closure(supervisor)
        mismatches = staging.verify_candidate_versions(
            candidate_python, closure.expected_versions()
        )
    except staging.StagingError as exc:
        transaction.fail_record(
            record, f"version probe failed: {exc}",
            root=transaction.DEFAULT_TX_ROOT,
        )
        try:
            shutil.rmtree(staging_root)
        except OSError:
            pass
        raise SystemExit(f"VERIFICATION FAILED: version probe: {exc}")
    if mismatches:
        transaction.fail_record(
            record,
            f"version mismatch: {' ; '.join(mismatches)}",
            root=transaction.DEFAULT_TX_ROOT,
        )
        try:
            shutil.rmtree(staging_root)
        except OSError:
            pass
        raise SystemExit(f"VERIFICATION FAILED: {', '.join(mismatches)}")
    transaction.advance(record, "candidate_verified", root=transaction.DEFAULT_TX_ROOT)
    transaction.save(record, root=transaction.DEFAULT_TX_ROOT)
    _emit({
        "status": "candidate_verified",
        "tx_id": args.tx_id,
        "staging_root": str(staging_root),
        "expected_sha": ACCEPTED_ARTIFACT_SHA,
        "expected_version": ACCEPTED_ARTIFACT_VERSION,
        "record": record.to_dict(),
    })
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

    p_rollback = sub.add_parser("rollback", help="Pair-rollback a failed transaction")
    p_rollback.add_argument("--tx-id", required=True)
    p_rollback.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    p_rollback.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    p_rollback.set_defaults(func=cmd_rollback)

    p_stage = sub.add_parser(
        "stage",
        help="Stage a complete, verified candidate runtime for an existing tx",
    )
    p_stage.add_argument("--tx-id", required=True)
    p_stage.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    p_stage.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    p_stage.set_defaults(func=cmd_stage)

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

    p_reconcile = sub.add_parser(
        "reconcile-stale",
        help=(
            "Reconcile a stale transaction. Inspects the historical "
            "transaction record and the current filesystem state, "
            "and quarantines the candidate if it is independently "
            "proven safe. The historical transaction record is "
            "NEVER modified; a new audit record is written."
        ),
    )
    p_reconcile.add_argument("--tx-id", required=True)
    p_reconcile.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    p_reconcile.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    p_reconcile.set_defaults(func=cmd_reconcile_stale)

    p_promote = sub.add_parser(
        "promote",
        help=(
            "REFUSED: this CLI does not expose a promote subcommand. "
            "Use deploy/scripts/peer_promote_o1_v3.py as the canonical "
            "host entrypoint."
        ),
    )
    p_promote.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    p_promote.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    p_promote.set_defaults(func=cmd_promote)

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
