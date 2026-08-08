#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
REFUSED: peer_promote_o1_v2.sh is permanently disabled for live Control Room promotion.

The v2 implementation is retained in Git history for incident analysis, but it
must not be executed. It had two unacceptable safety properties:

1. rollback selected an old runtime via a global venv.legacy-* search rather
   than the exact rollback path owned by the current transaction;
2. shell failure control flow did not guarantee paired runtime+DB rollback for
   every failure after the mutation boundary.

Use the reviewed v3 path instead:

  deploy/scripts/peer_promote_o1_v3.py --preflight-only
  deploy/scripts/peer_promote_o1_v3.py --promote

Hard invariant: O2 supervises O1 for this promotion. Never run a self-update.
EOF

exit 64
