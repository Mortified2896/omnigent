#!/usr/bin/env bash
# promote-omnigent-maintenance.sh — PERMANENTLY DISABLED refusal shim.
#
# This script is the original O1 promotion host wrapper. It was the
# proximate cause of the 2026-08-08 incident:
#
#   1. The preflight failed.
#   2. The fallback rollback ran anyway.
#   3. The rollback inferred ownership from path shape instead of
#      transaction record.
#   4. The active runtime /opt/omnigent/venv was deleted.
#
# The script is preserved in Git history for incident analysis. It
# is NOT a live promotion entrypoint. Running this script will not
# promote anything; it will refuse and exit non-zero.
#
# The canonical live promotion entrypoint is:
#
#   deploy/scripts/peer_promote_o1_v3.py
#
# Operators who reach for this script by accident will see the
# refusal below and a pointer to the v3 entrypoint with the exact
# arguments and architectural context.
#
# This is a hard refusal — there is no fallback path. The whole
# purpose of replacing this script is to make the unsafe fallback
# path structurally unreachable.

set -euo pipefail

cat >&2 <<'EOF'
REFUSED: promote-omnigent-maintenance.sh is permanently disabled for live
Control Room promotion.

This script is the original 2026-08-08 incident vector. It must not be
executed. It is preserved in Git history (see
deploy/scripts/promote-omnigent-maintenance.sh in earlier commits) for
incident analysis only.

The 2026-08-08 incident specifically:

  1. The preflight failed (missing executable preflight).
  2. The fallback rollback ran anyway.
  3. The rollback inferred ownership from path shape ("no venv.legacy-*
     -> current venv must be the new release") instead of consulting
     the transaction record.
  4. The rollback deleted /opt/omnigent/venv while the original 0.8.1
     runtime was still active.

The replacement:

  deploy/scripts/peer_promote_o1_v3.py --preflight-only
  deploy/scripts/peer_promote_o1_v3.py --promote

Hard architectural invariant:

  TARGET = O1, SUPERVISOR = O2.
  An instance never upgrades itself. The healthy peer supervises
  the entire operation.

The v3 entrypoint enforces:
  * TARGET/SUPERVISOR are non-configurable constants.
  * Every mutable phase is preceded by a strict preflight.
  * Every mutated resource is recorded under a transaction ID.
  * Rollback is restricted to resources owned by the transaction.
  * Rollback is restricted to resources the transaction actually
    created; unowned paths are refused.
  * Pre-mutation failures cannot touch the active runtime.
  * Post-mutation failures pair the runtime restore with the DB
    restore.
  * The historical transaction record is forensic evidence; it is
    never rewritten to manufacture ownership.
  * A first-class reconcile-stale operation exists for the
    reconciliation of historic failed transactions.

If the goal is to perform the O2-supervised O1 promotion to the
exact accepted 0.9 artifact, the host operator must run:

  sudo -E deploy/scripts/peer_promote_o1_v3.py --preflight-only
  sudo -E deploy/scripts/peer_promote_o1_v3.py --promote

If the goal is to reconcile a stale historical transaction (e.g.
promotion-20260808T201637Z-60ced75e from the 2026-08-08 incident),
the host operator must run:

  sudo -E deploy/scripts/peer_promote_o1_v3.py --reconcile-stale \
      promotion-20260808T201637Z-60ced75e

The reconciler inspects the historical transaction record and the
current filesystem state, independently proves the candidate is
safe to quarantine, and moves it into a per-transaction quarantine
directory. The historical transaction record is NEVER modified.

If neither of the above matches the goal, the operator must STOP
and inspect the deployment state before doing anything else.
EOF

exit 64
