#!/usr/bin/env python3
"""Host entrypoint for the approved Control Room O2 -> O1 promoter v3."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from peer_deployer.host_promotion import PromotionError, install_signal_handlers, run  # noqa: E402

DEFAULT_EVIDENCE = Path(
    "/var/lib/omnigent-production/upstream-0.9-candidate-20260808T100458Z/"
    "18-o1-peer-promotion-v3"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="O2-supervised O1 promotion v3")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--promote", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args(argv)
    install_signal_handlers()
    try:
        return run(args.evidence_dir, promote=args.promote)
    except PromotionError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
