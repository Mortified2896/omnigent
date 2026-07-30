#!/usr/bin/env bash
# Install helper for the external self-update controller (issue #38).
#
# Creates a durable updater installation outside the release tree:
#
#     /opt/omnigent/updater/
#         venv/                          # frozen Python venv
#         src/                           # checked-out tree (symlink or copy)
#         bin/omnigent-updater           # CLI entry point
#         bin/omnigent-updater.sh        # shell wrapper
#         etc/omnigent-updater.service   # systemd unit template
#         etc/omnigent-updater.env       # optional env overrides
#         etc/sudoers.d/omnigent-updater # tightly scoped sudoers rule
#
# The install helper is **not** run automatically by any other
# script. Operators invoke it explicitly after reading the runbook
# (``docs/deployments/omnigent-updater.md``). For staging
# acceptance tests, the helper is invoked with ``--prefix <tmpdir>``
# so production paths are never touched.
#
# Usage:
#   scripts/install_omnigent_updater.sh [--prefix <dir>]
#                                       [--service-unit]
#                                       [--sudoers-rule]
#
# Flags:
#   --prefix <dir>      Install under <dir> instead of /opt/omnigent/updater.
#                        Tests always pass this; production operators usually
#                        accept the default.
#   --service-unit      Also install /etc/systemd/system/omnigent-updater.service
#                        from the bundled template. OFF by default because
#                        issue #38 forbids activating the production unit
#                        until #29 closes.
#   --sudoers-rule      Also install /etc/sudoers.d/omnigent-updater with the
#                        tightly scoped rule. OFF by default — operators
#                        review the rule before installing it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PREFIX="/opt/omnigent/updater"
INSTALL_SERVICE=0
INSTALL_SUDOERS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --service-unit)
            INSTALL_SERVICE=1
            shift
            ;;
        --sudoers-rule)
            INSTALL_SUDOERS=1
            shift
            ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

log() { printf '[install] %s\n' "$*" >&2; }
fail() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null || fail "uv not found in PATH; install it from https://docs.astral.sh/uv/"

log "install root: $PREFIX"
mkdir -p "$PREFIX/bin" "$PREFIX/etc" "$PREFIX/etc/sudoers.d"

# Symlink the source tree. The updater is fully resolved at
# install time — future updates to the source tree require a
# re-install (the runbook calls this out).
if [[ ! -e "$PREFIX/src" ]]; then
    ln -s "$REPO_ROOT" "$PREFIX/src"
    log "linked $PREFIX/src -> $REPO_ROOT"
fi

# Frozen venv. We bootstrap with the system uv so the venv
# always exists when the operator invokes the wrapper. When
# running in CI / acceptance tests the venv may already exist;
# reuse it to keep the install idempotent.
if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
    log "creating frozen venv at $PREFIX/venv"
    uv venv --python 3.12 "$PREFIX/venv" || fail "uv venv failed"
    if [[ -f "$REPO_ROOT/uv.lock" ]]; then
        REQS="$(mktemp -t omnigent-updater-reqs-XXXXXX.txt)"
        (cd "$REPO_ROOT" && uv export --no-dev --format requirements-txt) > "$REQS"
        uv pip install --python "$PREFIX/venv/bin/python" -r "$REQS" \
            || fail "uv pip install (deps) failed"
        rm -f "$REQS"
    fi
    (cd "$REPO_ROOT" && uv pip install --python "$PREFIX/venv/bin/python" --no-deps .) \
        || fail "uv pip install . failed (non-editable install of updater)"
fi

# CLI entry point.
cat > "$PREFIX/bin/omnigent-updater" <<'PY'
#!/usr/bin/env python3
"""Thin entry point installed by scripts/install_omnigent_updater.sh."""
from omnigent.updater.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
PY
chmod 0755 "$PREFIX/bin/omnigent-updater"
log "installed $PREFIX/bin/omnigent-updater"

# Shell wrapper.
install -m 0755 "$SCRIPT_DIR/omnigent_updater.sh" "$PREFIX/bin/omnigent_updater.sh"
log "installed $PREFIX/bin/omnigent_updater.sh"

# Drop-in writer wrapper. The promote/rollback scripts use sudo to
# invoke this wrapper instead of the release's python directly so
# the sudoers rule can be narrowly scoped to a single binary. The
# wrapper validates SHA + release-dir before invoking the release
# python.
install -m 0750 "$SCRIPT_DIR/write-dropin.sh" "$PREFIX/bin/write-dropin.sh"
chown root:omnigent-updater "$PREFIX/bin/write-dropin.sh" 2>/dev/null || true
log "installed $PREFIX/bin/write-dropin.sh"

# Optional artifacts: systemd unit template + sudoers rule.
install -m 0644 "$REPO_ROOT/deploy/systemd/omnigent-updater.service.template" \
    "$PREFIX/etc/omnigent-updater.service.template"
log "installed $PREFIX/etc/omnigent-updater.service.template"

install -m 0644 "$REPO_ROOT/deploy/systemd/omnigent-updater.sudoers" \
    "$PREFIX/etc/sudoers.d/omnigent-updater"
log "installed $PREFIX/etc/sudoers.d/omnigent-updater"

if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
    if [[ "$EUID" -ne 0 ]]; then
        fail "--service-unit requires root; run with sudo"
    fi
    log "installing systemd unit to /etc/systemd/system/omnigent-updater.service"
    sed "s|@PREFIX@|$PREFIX|g" "$PREFIX/etc/omnigent-updater.service.template" \
        > /etc/systemd/system/omnigent-updater.service
    chmod 0644 /etc/systemd/system/omnigent-updater.service
    log "NOT activating the unit; issue #38 forbids unattended production activation"
fi

if [[ "$INSTALL_SUDOERS" -eq 1 ]]; then
    if [[ "$EUID" -ne 0 ]]; then
        fail "--sudoers-rule requires root; run with sudo"
    fi
    log "installing sudoers rule to /etc/sudoers.d/omnigent-updater"
    sed "s|@PREFIX@|$PREFIX|g" "$PREFIX/etc/sudoers.d/omnigent-updater" \
        > /etc/sudoers.d/omnigent-updater
    chmod 0440 /etc/sudoers.d/omnigent-updater
    log "sudoers rule installed"
fi

log "install complete"
log "verify with: $PREFIX/bin/omnigent-updater.sh list"
