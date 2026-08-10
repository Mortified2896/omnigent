#!/usr/bin/env bash
# ONE-TIME Control Room peer-deployer bootstrap installer.
#
# Operator MUST run this exactly once as root:
#
#     sudo bash /var/lib/omnigent-production/permanent-peer-deployer-bootstrap-<stamp>/bootstrap-installer.sh
#
# This wrapper is the *exact* committed wrapper that ships in the
# bootstrap handoff directory.  The committed wrapper is copied into
# the handoff by deploy/scripts/build_control_room_peer_deployer_bootstrap.py
# so the operator never runs a wrapper that has been edited in flight.
#
# Canonical handoff layout (the wrapper refuses to proceed if any of
# these is missing or is a symlink):
#
#     <handoff-dir>/
#         bootstrap-installer.sh
#         peer-deployer-package/
#             peer-deployer-package.tar.gz
#             bootstrap-manifest.json
#             PACKAGE.json
#             SHA256SUMS
#
# Two integrity layers, explicitly distinct:
#
#   A. OUTER handoff integrity (this wrapper, before sudo-side mutation)
#      - wrapper resolves its own location via BASH_SOURCE
#      - all paths are absolute, derived from the wrapper's own dir
#      - PACKAGE.json is parsed and the schema/layout version is enforced
#      - SHA256SUMS is verified against the tarball AND against the
#        outer bootstrap-manifest.json
#      - the tarball structure is sanity-checked for: no absolute paths,
#        no .., no symlink escapes, no device nodes, unexpected top-level
#      - the outer manifest is parsed as schema v2
#      - manifest.build_id must equal the build_id declared in PACKAGE.json
#      - the inner manifest embedded in the tarball must equal the outer
#        manifest bytes (ortho-checksum)
#
#   B. INNER source payload integrity (the bootstrap module, after
#      the wrapper passes the tarball to it)
#      - the bootstrap module verifies the extracted payload against
#        bootstrap-manifest.json
#      - this is the same manifest the wrapper already verified above
#      - the canonical path is the OUTER peer-deployer-package/bootstrap-manifest.json
#        which the wrapper copies into the unpacked source tree as
#        ./bootstrap-manifest.json
#
# --verify-only:
#   run the OUTER (A) checks and stop before any persistent mutation.
#   Extracts the tarball to a disposable tmp directory to prove the
#   inner payload verifies against the outer manifest, then deletes
#   the tmp directory.  Does NOT create /opt/control-room-peer-deployer,
#   /var/lib/control-room-peer-deployer, /run/control-room-peer-deployer,
#   does NOT install any systemd unit, does NOT daemon-reload, does NOT
#   restart anything.
#
# This wrapper depends on NOTHING from the operator's environment
# except a root shell, bash, and standard /usr/bin utilities.  It
# resolves its own directory from BASH_SOURCE[0] and uses absolute
# paths only.

set -euo pipefail

# ---------------------------------------------------------------------------
# Path resolution: derive every path from the wrapper's own location.
# ---------------------------------------------------------------------------
SELF_PATH="${BASH_SOURCE[0]}"
if [ ! -f "$SELF_PATH" ]; then
    # Fallback when invoked via 'bash <path>' where BASH_SOURCE is
    # the path passed to bash.
    if [ "$#" -ge 1 ]; then
        SELF_PATH="$1"
    fi
fi
SELF_DIR="$(cd "$(dirname "$SELF_PATH")" && pwd -P)"
SELF_BASE="$(basename "$SELF_PATH")"

# Verify the wrapper is a regular file at the canonical path.  If
# someone hands us a symlink or a relative path, refuse.
if [ -L "$SELF_PATH" ]; then
    echo "REFUSED: wrapper is a symlink; refusing to run: $SELF_PATH" >&2
    exit 2
fi
if [ ! -f "$SELF_DIR/$SELF_BASE" ]; then
    echo "REFUSED: wrapper is not a regular file: $SELF_DIR/$SELF_BASE" >&2
    exit 2
fi

PKG_DIR="$SELF_DIR/peer-deployer-package"
WRAPPER="$SELF_DIR/$SELF_BASE"
TARBALL="$PKG_DIR/peer-deployer-package.tar.gz"
MANIFEST="$PKG_DIR/bootstrap-manifest.json"
PACKAGE_JSON="$PKG_DIR/PACKAGE.json"
SHA256SUMS="$PKG_DIR/SHA256SUMS"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
VERIFY_ONLY=0
case "${1:-}" in
    --verify-only|-V)
        VERIFY_ONLY=1
        shift
        ;;
esac

log() { printf '[peer-deployer-bootstrap] %s\n' "$*"; }
fail() { printf '[peer-deployer-bootstrap] REFUSED: %s\n' "$*" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Preflight: package directory + required files (regular, not symlinks)
# ---------------------------------------------------------------------------
log "handoff dir: $SELF_DIR"
if [ ! -d "$PKG_DIR" ] || [ -L "$PKG_DIR" ]; then
    fail "missing peer-deployer-package/ directory: $PKG_DIR"
fi
for f in "$WRAPPER" "$TARBALL" "$MANIFEST" "$PACKAGE_JSON" "$SHA256SUMS"; do
    if [ -L "$f" ]; then
        fail "package member is a symlink (must be regular file): $f"
    fi
    if [ ! -f "$f" ]; then
        fail "missing required package file: $f"
    fi
done

# ---------------------------------------------------------------------------
# PACKAGE.json parse + layout version + build_id
# ---------------------------------------------------------------------------
log "verifying PACKAGE.json"
SELF_PY="/opt/omnigent-production/current/venv/bin/python"
if [ ! -x "$SELF_PY" ]; then
    SELF_PY="$(command -v python3.12 || true)"
fi
if [ ! -x "$SELF_PY" ] && [ -x /home/hermes/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/bin/python3.12 ]; then
    SELF_PY=/home/hermes/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/bin/python3.12
fi
if [ ! -x "$SELF_PY" ]; then
    SELF_PY="$(command -v python3 || true)"
fi
if [ ! -x "$SELF_PY" ]; then
    fail "no acceptable python interpreter found on this host"
fi

PACKAGE_BLOB="$("$SELF_PY" - "$PACKAGE_JSON" <<'PY'
import json, sys
blob = json.loads(open(sys.argv[1]).read())
print(blob.get("schema", ""))
print(blob.get("manifest_schema", ""))
print(blob.get("build_id", ""))
print(" ".join(blob.get("expected_outer_paths", [])))
# tally required files
expected = blob.get("expected_outer_paths", [])
for rel in ("bootstrap-installer.sh",
            "peer-deployer-package/peer-deployer-package.tar.gz",
            "peer-deployer-package/bootstrap-manifest.json",
            "peer-deployer-package/PACKAGE.json",
            "peer-deployer-package/SHA256SUMS"):
    print("1" if rel in expected else "0")
PY
)"

SCHEMA=$(echo "$PACKAGE_BLOB" | sed -n 1p)
MANIFEST_SCHEMA=$(echo "$PACKAGE_BLOB" | sed -n 2p)
PACKAGE_BUILD_ID=$(echo "$PACKAGE_BLOB" | sed -n 3p)
EXPECTED_PATHS=$(echo "$PACKAGE_BLOB" | sed -n 4p)
EXPECTED_FLAGS=$(echo "$PACKAGE_BLOB" | sed -n '5,9p')

[ "$SCHEMA" = "control-room-peer-deployer.bootstrap-package.v1" ] \
    || fail "PACKAGE.json schema mismatch: expected control-room-peer-deployer.bootstrap-package.v1, got '$SCHEMA'"
[ "$MANIFEST_SCHEMA" = "control-room-peer-deployer.bootstrap-manifest.v2" ] \
    || fail "PACKAGE.json manifest_schema mismatch: expected ...bootstrap-manifest.v2, got '$MANIFEST_SCHEMA'"
[ -n "$PACKAGE_BUILD_ID" ] || fail "PACKAGE.json missing build_id"
echo "$EXPECTED_FLAGS" | grep -q '^0$' && fail "PACKAGE.json does not list a required outer path"
log "PACKAGE.json schema OK, build_id=$PACKAGE_BUILD_ID"

# ---------------------------------------------------------------------------
# SHA256SUMS verification
# ---------------------------------------------------------------------------
log "verifying SHA256SUMS"
EXPECTED_TARBALL_SHA=$(awk '/peer-deployer-package.tar.gz$/ {print $1}' "$SHA256SUMS")
EXPECTED_MANIFEST_SHA=$(awk '/bootstrap-manifest.json$/ {print $1}' "$SHA256SUMS")
[ -n "$EXPECTED_TARBALL_SHA" ] || fail "SHA256SUMS missing tarball entry"
[ -n "$EXPECTED_MANIFEST_SHA" ] || fail "SHA256SUMS missing manifest entry"
ACTUAL_TARBALL_SHA=$(sha256sum "$TARBALL" | awk '{print $1}')
ACTUAL_MANIFEST_SHA=$(sha256sum "$MANIFEST" | awk '{print $1}')
[ "$EXPECTED_TARBALL_SHA" = "$ACTUAL_TARBALL_SHA" ] \
    || fail "tarball SHA-256 mismatch: expected $EXPECTED_TARBALL_SHA, got $ACTUAL_TARBALL_SHA"
[ "$EXPECTED_MANIFEST_SHA" = "$ACTUAL_MANIFEST_SHA" ] \
    || fail "manifest SHA-256 mismatch: expected $EXPECTED_MANIFEST_SHA, got $ACTUAL_MANIFEST_SHA"
log "SHA256SUMS verified (tarball=$ACTUAL_TARBALL_SHA, manifest=$ACTUAL_MANIFEST_SHA)"

# ---------------------------------------------------------------------------
# Tarball structure sanity (no path traversal, no symlinks, no devices,
# no absolute paths, expected top-level structure)
# ---------------------------------------------------------------------------
log "verifying tarball structure"
TAR_ENTRIES=$(tar -tzf "$TARBALL")
echo "$TAR_ENTRIES" | grep -E '^/' && fail "tarball contains absolute paths"
echo "$TAR_ENTRIES" | grep -E '(^|/)(\.\.)(/|$)' && fail "tarball contains '..' traversal"
echo "$TAR_ENTRIES" | grep -E '^./$' >/dev/null \
    || fail "tarball missing top-level './' entry"
echo "$TAR_ENTRIES" | grep -E '^./bootstrap-manifest\.json$' >/dev/null \
    || fail "tarball missing embedded ./bootstrap-manifest.json"
echo "$TAR_ENTRIES" | grep -E 'links to|link to' && fail "tarball contains (broken) symlink entries"
EXPECTED_FROM_PKG="bootstrap-installer.sh"
echo "$TAR_ENTRIES" | grep -E '^./deploy/' >/dev/null \
    || fail "tarball missing deploy/ tree"
# Verify the tarball contains no device nodes / FIFOs / symlinks by
# inspecting the verbose listing.
BAD_TYPES=$(tar -tvzf "$TARBALL" | awk '$1 ~ /[bcdlp]/ && $NF != "./" {print $1, $NF}')
if [ -n "$BAD_TYPES" ]; then
    fail "tarball contains non-regular entries: $BAD_TYPES"
fi
log "tarball structure OK"

# ---------------------------------------------------------------------------
# Outer manifest schema check (lightweight parse-without-extract)
# ---------------------------------------------------------------------------
log "verifying outer manifest schema"
"$SELF_PY" - "$MANIFEST" <<'PY' || fail "outer manifest schema check failed"
import json, re, sys
m = json.loads(open(sys.argv[1]).read())
assert m.get("schema") == "control-room-peer-deployer.bootstrap-manifest.v2", m.get("schema")
assert isinstance(m.get("hashes"), dict)
assert isinstance(m.get("file_count"), int)
assert len(m["hashes"]) == m["file_count"]
for k, v in m["hashes"].items():
    assert re.fullmatch(r"[0-9a-f]{64}", v), ("bad-hash", k)
    assert "\\" not in k
    assert not k.startswith("/")
    assert ".." not in k.split("/")
sha1 = re.compile(r"[0-9a-f]{40}")
assert sha1.fullmatch(m.get("build_id") or ""), m.get("build_id")
print("manifest schema OK; build_id=", m["build_id"], "files=", m["file_count"])
PY

# build_id must match PACKAGE.json + match the Git commit the operator
# approved.  We record the build_id we expect and print it.
EXPECTED_BUILD_ID="$PACKAGE_BUILD_ID"

# ---------------------------------------------------------------------------
# Extract tarball to a disposable tmp directory and verify the inner
# manifest equals the outer manifest bytes.
# ---------------------------------------------------------------------------
WORK=$(mktemp -d /var/lib/omnigent-production/tmp/peer-deployer-bootstrap-verify.XXXX)
trap 'rm -rf "$WORK"' EXIT
log "extracting tarball to disposable tmp $WORK"
tar -xzf "$TARBALL" -C "$WORK"
if [ ! -f "$WORK/bootstrap-manifest.json" ]; then
    fail "tarball did not extract ./bootstrap-manifest.json"
fi
INNER_MANIFEST_SHA=$(sha256sum "$WORK/bootstrap-manifest.json" | awk '{print $1}')
OUTER_MANIFEST_SHA=$(sha256sum "$MANIFEST" | awk '{print $1}')
[ "$INNER_MANIFEST_SHA" = "$OUTER_MANIFEST_SHA" ] \
    || fail "outer manifest bytes differ from tarball embedded manifest: $INNER_MANIFEST_SHA vs $OUTER_MANIFEST_SHA"
log "inner manifest bytes match outer manifest bytes"

# Now run the canonical inner-source-payload verification exactly as
# the real bootstrap will.  We use the committed bootstrap module.
BOOTSTRAP_PY="$WORK/deploy/scripts/control_room_peer_deployer_bootstrap.py"
if [ ! -f "$BOOTSTRAP_PY" ]; then
    fail "tarball missing deploy/scripts/control_room_peer_deployer_bootstrap.py"
fi
log "running inner payload verification (committed bootstrap module)"
( cd "$WORK" && "$SELF_PY" - "$BOOTSTRAP_PY" <<'PY' ) || fail "inner payload verification failed"
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("bootstrap", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
work = pathlib.Path(".").resolve()
manifest = m._validate_manifest_schema(work / "bootstrap-manifest.json")
got = m._verify_payload_against_manifest(manifest, work)
print("inner payload verified:", len(got), "files")
PY

# build_id declared in the manifest must match PACKAGE.json
MANIFEST_BUILD_ID=$("$SELF_PY" -c "import json,sys; print(json.loads(open('$MANIFEST').read())['build_id'])" || true)
[ "$MANIFEST_BUILD_ID" = "$EXPECTED_BUILD_ID" ] \
    || fail "manifest.build_id '$MANIFEST_BUILD_ID' != PACKAGE.json build_id '$EXPECTED_BUILD_ID'"
log "manifest.build_id matches PACKAGE.json build_id ($MANIFEST_BUILD_ID)"

# ---------------------------------------------------------------------------
# Stop here if --verify-only was requested.
# ---------------------------------------------------------------------------
if [ "$VERIFY_ONLY" = "1" ]; then
    log "OK -- verify-only: tarball, manifest, PACKAGE.json, SHA256SUMS, inner payload integrity all verified."
    log "OK -- verify-only: no persistent state was created by this invocation."
    log "OK -- handoff: $SELF_DIR"
    log "OK -- wrapper:  $WRAPPER"
    log "OK -- tarball:  $TARBALL"
    log "OK -- manifest: $MANIFEST"
    log "OK -- tarball SHA-256:  $ACTUAL_TARBALL_SHA"
    log "OK -- manifest SHA-256: $ACTUAL_MANIFEST_SHA"
    exit 0
fi

# ---------------------------------------------------------------------------
# Privileged bootstrap: hand off to the bootstrap module.
# ---------------------------------------------------------------------------
if [ "$(id -u)" != "0" ]; then
    fail "bootstrap must be run as root (use sudo); current uid=$(id -u)"
fi

log "handing off to bootstrap module (privileged)"
cd "$WORK"
"$SELF_PY" deploy/scripts/control_room_peer_deployer_bootstrap.py \
    --source . \
    --manifest bootstrap-manifest.json

log "OK"
exit 0
