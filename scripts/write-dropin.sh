#!/usr/bin/env bash
set -euo pipefail
ACTION="$1"
SHA="$2"
RELEASE_DIR="$3"
# Refuse any arg that contains shell metacharacters.
case "$SHA" in
    *[\`\"\;\\\&\$\|\<\>\(\)\{\}\[\]]*) echo "bad SHA arg" >&2; exit 2 ;;
esac
case "$RELEASE_DIR" in
    /home/hermes/workspace/deployments/omnigent/releases/*) ;;
    *) echo "release dir outside approved root" >&2; exit 2 ;;
esac
case "$ACTION" in
    write|disable) ;;
    *) echo "unknown action" >&2; exit 2 ;;
esac
export SHA RELEASE_DIR ACTION
exec "$RELEASE_DIR/.venv/bin/python" -c "
import os, sys
sys.path.insert(0, os.environ[\"RELEASE_DIR\"])
from omnigent.deploy.ops.systemd import write_release_dropin, disable_other_release_dropins
from pathlib import Path
sha = os.environ[\"SHA\"]
release_dir = Path(os.environ[\"RELEASE_DIR\"])
if os.environ[\"ACTION\"] == \"write\":
    print(write_release_dropin(sha, release_dir=release_dir))
else:
    disable_other_release_dropins(sha)
" < /dev/null
