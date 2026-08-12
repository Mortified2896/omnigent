#!/usr/bin/env python3
"""Host entrypoint for the approved Control Room O2 -> O1 promoter v3.

This is the SINGLE live promotion entrypoint for the Control Room.
It exposes:

  * --preflight-only    run the strict preflight gate; no mutation
  * --promote            run the full promotion with paired rollback
  * --reconcile-stale    first-class reconciliation of a stale
                         transaction; quarantines the candidate if
                         it is independently proven safe. The
                         historical transaction record is NEVER
                         modified.

All other historical deploy scripts are hard-disabled refusal shims.
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

DEFAULT_EVIDENCE = Path(
    "/var/lib/omnigent-production/upstream-0.9-candidate-20260808T100458Z/"
    "19-final-peer-deployer-hardening"
)


def _cmd_reconcile_stale(tx_id: str, evidence_dir: Path) -> int:
    """Reconcile a stale transaction.

    Loads the historical transaction record READ-ONLY, independently
    proves the candidate is safe to quarantine (NOT active runtime,
    NOT O2, NOT referenced by any service), and MOVES the candidate
    into a per-transaction quarantine directory. The historical
    transaction record is NEVER modified.

    Refuses with exit 2 if any safety proof fails.
    """
    report = reconcile.reconcile_stale_transaction(
        tx_id,
        quarantine_root=reconcile.DEFAULT_QUARANTINE_ROOT,
        tx_root=transaction.DEFAULT_TX_ROOT,
        allowed_target=identity.O1,
        allowed_supervisor=identity.O2,
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    audit_path = evidence_dir / f"reconcile-{tx_id}.json"
    audit_path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.safe else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="O2-supervised O1 promotion v3")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--promote", action="store_true")
    mode.add_argument(
        "--reconcile-stale",
        metavar="TX_ID",
        help=(
            "Reconcile a stale transaction. Quarantines the candidate "
            "if it is independently proven safe. The historical "
            "transaction record is NEVER modified."
        ),
    )
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args(argv)
    install_signal_handlers()

    if args.reconcile_stale:
        try:
            return _cmd_reconcile_stale(args.reconcile_stale, args.evidence_dir)
        except reconcile.ReconciliationError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2

    try:
        return run(args.evidence_dir, promote=args.promote)
    except PromotionError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
