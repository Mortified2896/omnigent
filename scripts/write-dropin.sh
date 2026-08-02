#!/usr/bin/env bash
# Sudoers-gated wrapper around the systemd drop-in writer.
#
# This wrapper is what ``scripts/promote_release.sh`` and
# ``scripts/rollback_release.sh`` invoke under ``sudo`` to write the
# per-release drop-in for the omnigent-eval-web and omnigent-eval-host
# systemd units. The wrapper enforces four invariants the
# promotion / rollback scripts cannot enforce from a regular shell:
#
#   1. The service name must be one of an explicit allow-list
#      (web | host). No other service can be reconfigured by this
#      wrapper, so a malicious or buggy caller cannot use it to
#      rewrite drop-ins for unrelated systemd units.
#   2. The release directory must live under the approved root
#      ``/home/hermes/workspace/deployments/omnigent/releases/``.
#      This blocks a caller from pointing the unit at a different
#      tree (e.g. a developer checkout).
#   3. The SHA argument must not contain shell metacharacters. The
#      wrapper then runs ``python -c`` against the release's
#      interpreter, so a hostile SHA cannot break out of the
#      python invocation.
#   4. The action must be one of ``write`` or ``disable``. The
#      ``write`` action creates / replaces the drop-in; ``disable``
#      moves any sibling ``10-release-<other-sha>.conf`` to
#      ``.disabled`` so the new drop-in wins precedence.
#
# Usage:
#   write-dropin.sh write <service> <sha> <release-dir>
#   write-dropin.sh disable <service> <sha> <release-dir>
#
# The service argument selects which unit to reconfigure; ``web``
# writes / disables drop-ins under
# ``/etc/systemd/system/omnigent-eval-web.service.d/`` and ``host``
# does the same under
# ``/etc/systemd/system/omnigent-eval-host.service.d/``. Both
# services are pinned to the same release — this wrapper is the
# single point of truth for both drop-in trees.

set -euo pipefail

if [[ $# -lt 4 ]]; then
  echo "usage: $0 <write|disable> <web|host> <sha> <release-dir>" >&2
  exit 2
fi

ACTION="$1"
SERVICE_KIND="$2"
SHA="$3"
RELEASE_DIR="$4"

# 1. Allow-list the action.
case "$ACTION" in
    write|disable) ;;
    *) echo "unknown action: $ACTION" >&2; exit 2 ;;
esac

# 2. Allow-list the service.
case "$SERVICE_KIND" in
    web|host) ;;
    *) echo "unknown service kind: $SERVICE_KIND (expected web|host)" >&2; exit 2 ;;
esac

# 3. Refuse any SHA arg that contains shell metacharacters. The
# Python invocation reads SHA from the environment, so a metachar
# inside SHA could escape the python heredoc.
case "$SHA" in
    *[\`\"\;\\\&\$\|\<\>\(\)\{\}\[\]]*) echo "bad SHA arg" >&2; exit 2 ;;
esac

# 4. Pin the release directory to the approved root. Anything else
# is rejected before the Python interpreter is invoked.
case "$RELEASE_DIR" in
    /home/hermes/workspace/deployments/omnigent/releases/*) ;;
    *) echo "release dir outside approved root: $RELEASE_DIR" >&2; exit 2 ;;
esac

export SHA RELEASE_DIR ACTION SERVICE_KIND
exec "$RELEASE_DIR/.venv/bin/python" -c "
import os, sys
sys.path.insert(0, os.environ['RELEASE_DIR'])
from dataclasses import replace
from omnigent.deploy.ops.systemd import (
    write_release_dropin,
    disable_other_release_dropins,
    host_service_spec,
    web_service_spec,
)
from pathlib import Path

sha = os.environ['SHA']
release_dir = Path(os.environ['RELEASE_DIR'])
service_kind = os.environ['SERVICE_KIND']

if service_kind == 'host':
    spec = host_service_spec()
else:
    spec = web_service_spec()

if os.environ['ACTION'] == 'write':
    print(write_release_dropin(sha, release_dir=release_dir, spec=spec))
else:
    disable_other_release_dropins(sha, spec=spec)
" < /dev/null