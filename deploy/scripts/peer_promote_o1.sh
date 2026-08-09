#!/usr/bin/env bash
# peer_promote_o1.sh — PERMANENTLY DISABLED refusal shim.
#
# This script was the original "host-level counterpart" to the
# peer_deployer Python module. It was a 539-line bash deployer
# intended to be run by the host operator. The v3 Python entrypoint
# is the canonical live promotion path; this script is preserved
# in Git history for incident analysis.
#
# Run the v3 entrypoint instead:
#
#   deploy/scripts/peer_promote_o1_v3.py --preflight-only
#   deploy/scripts/peer_promote_o1_v3.py --promote

set -euo pipefail

cat >&2 <<'EOF'
REFUSED: peer_promote_o1.sh is permanently disabled for live Control Room
promotion.

The v3 Python entrypoint is the canonical live promotion path:

  deploy/scripts/peer_promote_o1_v3.py --preflight-only
  deploy/scripts/peer_promote_o1_v3.py --promote

Keep at most ONE supported live O1 promotion entrypoint. There is no
v1 anymore. The v3 entrypoint is the one. The v2 shell is a refusal
shim. This script is also a refusal shim.

The brief's architectural invariant stands:

  O1 upgrades O2; O2 upgrades O1.
  An instance never normally upgrades itself.
  TARGET = O1, SUPERVISOR = O2.
EOF

exit 64
