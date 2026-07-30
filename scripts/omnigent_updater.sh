#!/usr/bin/env bash
# Operator shell wrapper for ``omnigent-updater``.
#
# Lives at ``/opt/omnigent/updater/bin/omnigent-updater.sh`` after
# the install helper runs. Symlinks to ``omnigent-updater`` so the
# operator can invoke either name.
#
# This wrapper intentionally contains no business logic — every
# command line flag is forwarded to ``omnigent.updater.cli.main``.
# Tests + the operator runbook rely on this so a future change to
# the Python module does not require shell edits.
#
# Usage:
#   omnigent-updater.sh record --target-sha <sha> --expected-current-sha <sha>
#                              [--origin-session-id SID] [--origin-conversation-id CID]
#                              [--operator NAME] [--notes "..."]
#   omnigent-updater.sh status <request_id>
#   omnigent-updater.sh run <request_id> [--dry-run]
#   omnigent-updater.sh list
#   omnigent-updater.sh recover
#   omnigent-updater.sh sweep-locks [--stale-seconds N]
#   omnigent-updater.sh show-result <request_id>

set -euo pipefail

# Resolve the updater install root. ``OMNIGENT_UPDATER_HOME``
# overrides discovery so the wrapper can be invoked from a
# different install (e.g. the staging acceptance harness).
if [[ -n "${OMNIGENT_UPDATER_HOME:-}" ]]; then
    INSTALL_ROOT="${OMNIGENT_UPDATER_HOME}"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    INSTALL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

VENV_PY="$INSTALL_ROOT/venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    echo "error: $VENV_PY not found; check the updater install at $INSTALL_ROOT" >&2
    exit 2
fi

exec "$VENV_PY" -m omnigent.updater.cli "$@"
