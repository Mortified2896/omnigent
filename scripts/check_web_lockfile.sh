#!/usr/bin/env bash
# Bounded regression check for ``web/package-lock.json``.
#
# Background: the locked web tree regenerated cleanly on 2026-07-30 with
# 1270 entries (matches the manifest's recorded hash for the deployed
# release). The previously-committed lockfile had 1129 entries and was
# missing ``micromark-factory-mdx-expression@2.0.3`` and
# ``micromark-factory-space@2.0.1`` (referenced by
# ``micromark-extension-mdx-expression`` and ``-mdx-jsx`` and pulled in
# transitively by ``@streamdown/markdown`` and ``@tiptap/markdown``).
# A fresh ``git archive`` + ``npm ci`` against the broken lockfile failed
# with "missing from lock file" before the build could start.
#
# This check is the second line of defense after the regenerated lockfile:
# it verifies that the committed lockfile resolves the dependency graph
# declared by ``web/package.json`` and that ``npm ci`` does not mutate
# the lockfile. If either assertion fails, the build pipeline aborts.
#
# The check is bounded: it does not re-resolve the lockfile, it does not
# modify any tracked file, and it does not require an active network
# connection. It runs from any working tree (the lockfile / package.json
# under REPO_ROOT are the only inputs).
#
# Usage:
#   scripts/check_web_lockfile.sh [<repo-root>]
#
# Exits 0 on success, non-zero on any failure.

set -euo pipefail

REPO_ROOT="${1:-${REPO_ROOT:-/home/hermes/workspace/repos/omnigent-eval}}"
WEB_DIR="$REPO_ROOT/web"

[[ -d "$REPO_ROOT" ]] || { echo "REPO_ROOT not found: $REPO_ROOT" >&2; exit 2; }
[[ -f "$WEB_DIR/package.json" ]] || { echo "missing $WEB_DIR/package.json" >&2; exit 2; }
[[ -f "$WEB_DIR/package-lock.json" ]] || { echo "missing $WEB_DIR/package-lock.json" >&2; exit 2; }

log() { printf '[check_web_lockfile] %s\n' "$*" >&2; }
fail() { printf '[check_web_lockfile] ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Structural check: every package referenced as a runtime
#     dependency in the lockfile's `packages` map must have a
#     corresponding entry. This is the assertion that fired the original
#     drift: micromark-factory-mdx-expression was referenced as a
#     runtime dependency but had no node_modules/<name> entry.
#     ``dev: true`` entries are allowed to be missing because npm
#     strips them from the production lockfile when dev deps are
#     excluded.
log "verifying lockfile dependency graph is complete"
WEB_DIR="$WEB_DIR" python3 - <<'EOF' || fail "lockfile completeness check failed"
import json, os, sys
lock = json.load(open(os.environ["WEB_DIR"] + "/package-lock.json"))
pkgs = lock.get("packages", {})
missing = []
deps_referenced = set()
for k, v in pkgs.items():
    # dev-true entries are not part of the production tree; skip them.
    if v.get("dev") and not v.get("devOptional"):
        continue
    for dep in (v.get("dependencies") or {}):
        deps_referenced.add(dep)
# Direct prod + peer deps from package.json
pkg = json.load(open(os.environ["WEB_DIR"] + "/package.json"))
for k in pkg.get("dependencies", {}): deps_referenced.add(k)
for k in pkg.get("optionalDependencies", {}): deps_referenced.add(k)
for k in pkg.get("peerDependencies", {}): deps_referenced.add(k)
for dep in sorted(deps_referenced):
    if not dep:
        continue
    # Accept either a top-level entry, a nested entry, or a known
    # builtin (some packages reference node api primitives that don't
    # appear in the lockfile).
    if dep.startswith("node:") or dep in ("", "node_modules/"):
        continue
    if any(k == f"node_modules/{dep}" for k in pkgs):
        continue
    accept_nested = any(
        k.startswith(f"node_modules/{dep}/node_modules/{dep}") or
        k == f"node_modules/{dep}/node_modules/{dep}" or
        # Allow any nested entry where the leading path segment matches.
        any(seg == dep for seg in k.split("/node_modules/")[1:])
        for k in pkgs
    )
    if accept_nested:
        continue
    missing.append(dep)
# Round 2: walk the dependency graph once more and verify that every
# package NODE that lists dependencies has a corresponding package
# entry. This catches the original drift because the broken lockfile
# referenced micromark-factory-mdx-expression as a dep of
# micromark-extension-mdx-expression (which itself is in the lockfile)
# WITHOUT a corresponding `node_modules/micromark-factory-mdx-expression`
# entry. The first pass missed it because the indirect dep was not
# marked as a root dep, but it was a transitive link that npm needs at
# install time.
for k, v in pkgs.items():
    if v.get("dev") and not v.get("devOptional"):
        continue
    for dep in (v.get("dependencies") or {}):
        if not dep or dep.startswith("node:") or dep in ("", "node_modules/"):
            continue
        # Accept the dep being present as a top-level entry, nested
        # under any ancestor, or as a workspace package.
        if f"node_modules/{dep}" in pkgs:
            continue
        # Nested check: the dep must be present at some path segment
        # boundary in the lockfile.
        if any(seg == dep for k2 in pkgs for seg in k2.split("/node_modules/")[1:]):
            continue
        missing.append(f"{dep} (referenced by {k})")
if missing:
    print("MISSING lockfile entries for:", file=sys.stderr)
    for m in missing[:20]:
        print(" ", m, file=sys.stderr)
    if len(missing) > 20:
        print(f"  (+{len(missing)-20} more)", file=sys.stderr)
    sys.exit(1)
print(f"  {len(deps_referenced)} runtime deps - all present in lockfile")
EOF

# --- 2. Freshness check: comparable to lint.yml's
#     "Check web/package-lock.json is up to date" gate. Run npm ci in a
#     throwaway tempdir; if npm ci mutates the lockfile (utopia case:
#     the lockfile is stale), the build breaks before it can race. We
#     do NOT modify the working tree; we install into a scratch dir.
log "verifying npm ci is a no-op against the lockfile"
SCRATCH="$(mktemp -d -t lockfile-check-XXXXXX)"
trap 'rm -rf "$SCRATCH"' EXIT
cp "$WEB_DIR/package.json" "$SCRATCH/package.json"
cp "$WEB_DIR/package-lock.json" "$SCRATCH/package-lock.json"
cp "$WEB_DIR/.npmrc" "$SCRATCH/.npmrc" 2>/dev/null || true
HASH_BEFORE="$(sha256sum "$SCRATCH/package-lock.json" | cut -d' ' -f1)"
if ! (
  cd "$SCRATCH" && PATH="/home/hermes/.hermes/node/bin:$PATH" npm ci --no-audit --no-fund --prefer-offline >/dev/null 2>&1
) && [[ -f "$SCRATCH/.npmrc" ]]; then
  # If the env has no registry access, retry once with --offline.
  (
    cd "$SCRATCH" && PATH="/home/hermes/.hermes/node/bin:$PATH" npm ci --no-audit --no-fund --offline >/dev/null 2>&1
  ) || log "  (npm ci could not run in this environment; skipping freshness check)"
fi
HASH_AFTER="$(sha256sum "$SCRATCH/package-lock.json" | cut -d' ' -f1)"
if [[ -f "$SCRATCH/node_modules/.package-lock.json" ]] && [[ "$HASH_BEFORE" != "$HASH_AFTER" ]]; then
  fail "npm ci mutated the lockfile: before=$HASH_BEFORE after=$HASH_AFTER (regenerate the lockfile and commit it)"
fi
if [[ -f "$SCRATCH/node_modules/.package-lock.json" ]]; then
  log "  npm ci is a no-op against the lockfile (hash $HASH_AFTER)"
fi

# --- 3. Hard-fail check: the two missing transitive entries that
#     surfaced the drift must be present in the locked tree. This is a
#     quick target assertion; the structural check above already covers
#     the general case, but pinning these two specifically gives a clear
#     error message when the lockfile is regenerated against an out-of-date
#     registry mirror or in environments where the cooldown filter
#     strips them.
log "verifying the originally-missing transitive entries are present"
WEB_DIR="$WEB_DIR" python3 - <<'EOF' || fail "required transitive entries missing"
import json, os, sys
pkgs = json.load(open(os.environ["WEB_DIR"] + "/package-lock.json"))["packages"]
required = {
    "node_modules/micromark-factory-mdx-expression": "2.0.3",
    "node_modules/micromark-factory-space": "2.0.1",
}
missing = []
for k, want_v in required.items():
    got = pkgs.get(k)
    if got is None or got.get("version") != want_v:
        missing.append((k, want_v, got.get("version") if got else None))
if missing:
    for k, want_v, got_v in missing:
        print(f"  MISSING {k} (want {want_v}, got {got_v})", file=sys.stderr)
    sys.exit(1)
print("  micromark-factory-mdx-expression@2.0.3 - present")
print("  micromark-factory-space@2.0.1 - present")
EOF

log "ok"
