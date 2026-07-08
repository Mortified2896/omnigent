#!/usr/bin/env bash
# One-shot pre/post session-switch benchmark for PR descriptions.
#
# Swaps main's chatStore.ts for the PRE run, restores the branch copy for POST,
# then prints a markdown comparison table. Safe on a dirty tree: only touches
# web/src/store/chatStore.ts (backed up to /tmp).
#
# Usage (from repo root):
#   ./loadtest/session_switch_compare.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$ROOT/web"
CHAT_STORE="$WEB/src/store/chatStore.ts"
BACKUP="/tmp/omnigent-chatStore.post.ts"
RUNS="${WEB_LATENCY_RUNS:-3}"
OUT_DIR="${TMPDIR:-/tmp}/session-switch-bench"
mkdir -p "$OUT_DIR"

if [[ ! -f "$CHAT_STORE" ]]; then
  echo "error: $CHAT_STORE not found" >&2
  exit 1
fi

cp "$CHAT_STORE" "$BACKUP"

run_bench() {
  local label="$1"
  local outfile="$OUT_DIR/${label}.json"
  (
    cd "$WEB"
    WEB_LATENCY_BENCH=1 \
    WEB_LATENCY_MODE=session_switch \
    WEB_LATENCY_SCENARIO=slow_snapshot \
    WEB_LATENCY_RUNS="$RUNS" \
    WEB_LATENCY_LABEL="$label" \
    npm test -- --run src/loadtest/sessionSwitchBench.run.test.ts 2>/dev/null \
      | tee "$OUT_DIR/${label}.log" \
      | rg 'WEB_LATENCY_JSON' \
      | tail -1 \
      | sed 's/^WEB_LATENCY_JSON //' > "$outfile"
  )
  if [[ ! -s "$outfile" ]]; then
    echo "error: bench produced no output for $label (see $OUT_DIR/${label}.log)" >&2
    exit 1
  fi
}

echo "==> PRE (main chatStore.ts)"
git show main:"web/src/store/chatStore.ts" > "$CHAT_STORE"
run_bench pre

echo "==> POST (branch chatStore.ts)"
cp "$BACKUP" "$CHAT_STORE"
run_bench post

python3 - "$OUT_DIR/pre.json" "$OUT_DIR/post.json" "$RUNS" <<'PY'
import json
import sys

pre_path, post_path, runs = sys.argv[1:4]
pre = json.load(open(pre_path))
post = json.load(open(post_path))

def row(name, key, lower_better=True):
    a, b = pre[key], post[key]
    if a == 0:
        delta = "n/a"
    else:
        pct = (b - a) / a * 100
        sign = "-" if lower_better and b < a else ("+" if b > a else "")
        delta = f"{sign}{abs(pct):.0f}%"
    return f"| {name} | {a:.0f} | {b:.0f} | {delta} |"

print()
print(f"### Session-switch latency (vitest, slow snapshot, n={runs} p50)")
print()
print(f"Scenario: history {pre['historyDelayMs']}ms / snapshot {pre['snapshotDelayMs']}ms simulated delay.")
print()
print("| Metric | main (pre) | branch (post) | Δ |")
print("|--------|------------|---------------|---|")
for name, key, lb in [
    ("historyHydratedMs (store gate)", "firstBubbleMs", True),
    ("metadataLagMs", "metadataLagMs", False),
    ("totalBindMs", "totalBindMs", True),
    ("historyFetchMs", "historyFetchMs", True),
    ("snapshotFetchMs", "snapshotFetchMs", True),
]:
    print(row(name, key, lb))
print()
print("_metadataLagMs increase on post is expected: messages paint before header metadata._")
PY

echo "==> Done. Raw JSON: $OUT_DIR/pre.json $OUT_DIR/post.json"
