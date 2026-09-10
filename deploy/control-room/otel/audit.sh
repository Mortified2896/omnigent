#!/bin/sh
set -eu
exec python3 "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/telemetry_audit.py" "$@"
