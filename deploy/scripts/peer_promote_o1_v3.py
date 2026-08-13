#!/usr/bin/env python3
"""Canonical host entrypoint for explicit peer-supervised promotion.

The filename is retained for operator continuity; the interface safely supports
both O1/O2 directions and both deployment modes. No target, supervisor,
artifact identity, or evidence directory is implicit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from peer_deployer import identity, reconcile, transaction  # noqa: E402
from peer_deployer.host_promotion import (  # noqa: E402
    PromotionError,
    install_signal_handlers,
    run,
)
from peer_deployer.mode import DeploymentMode  # noqa: E402


def _cmd_reconcile_stale(
    tx_id: str,
    evidence_dir: Path,
    *,
    target: identity.Instance,
    supervisor: identity.Instance,
) -> int:
    report = reconcile.reconcile_stale_transaction(
        tx_id,
        quarantine_root=reconcile.DEFAULT_QUARANTINE_ROOT,
        tx_root=transaction.DEFAULT_TX_ROOT,
        allowed_target=target,
        allowed_supervisor=supervisor,
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    audit_path = evidence_dir / f"reconcile-{tx_id}.json"
    audit_path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.safe else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit peer-supervised Control Room promotion")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight-only", action="store_true")
    action.add_argument("--promote", action="store_true")
    action.add_argument("--reconcile-stale", metavar="TX_ID")
    parser.add_argument("--target", required=True, choices=sorted(identity.REGISTRY))
    parser.add_argument("--supervisor", required=True, choices=sorted(identity.REGISTRY))
    parser.add_argument("--mode", choices=[item.value for item in DeploymentMode])
    parser.add_argument("--acceptance-record", type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    target = identity.get(args.target)
    supervisor = identity.get(args.supervisor)
    try:
        identity.require_distinct(target, supervisor)
    except identity.IdentityError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 5
    install_signal_handlers()

    if args.reconcile_stale:
        try:
            return _cmd_reconcile_stale(
                args.reconcile_stale,
                args.evidence_dir,
                target=target,
                supervisor=supervisor,
            )
        except reconcile.ReconciliationError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    if args.mode is None or args.acceptance_record is None:
        parser.error("--mode and --acceptance-record are required for promotion/preflight")
    try:
        return run(
            args.evidence_dir,
            promote=args.promote,
            target=target,
            supervisor=supervisor,
            mode=DeploymentMode.parse(args.mode),
            acceptance_record_path=args.acceptance_record,
        )
    except (PromotionError, identity.IdentityError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
