# Sourced by evaluate-checks.sh. This is the fork's authoritative Merge Ready
# contract. Keep it synchronized with the workflow job names; the offline
# contract test derives those names from the workflow definitions.
#
# Matrix, path-filtered, and draft-gated jobs are in ALLOW_SKIP because they can
# be legitimately absent. Security Scan and both lint jobs must report success.
# E2E UI Required remains advisory until the fork configures its judge model and
# gateway credentials. The mock E2E UI suite and deterministic UI Snapshot gate
# remain authoritative and require no secrets.

REQUIRED=(
  "Pre-commit checks"
  "Version lockstep check"
  "Security Scan"
  "Docker build"
  "web test"
  "Pytest (runtime-harnesses)"
  "Pytest (runtime-policies)"
  "Pytest (runtime-core)"
  "Pytest (inner-rest)"
  "Pytest (tools)"
  "Pytest (repl-sdk)"
  "Pytest (server-approvals)"
  "Pytest (server-integration)"
  "Pytest (server-rest)"
  "Pytest (spec-llms)"
  "Pytest (integration-mock)"
  "Pytest (runner-app)"
  "Pytest (stores)"
  "Pytest (misc)"
  "Pytest (databricks)"
  "Pytest (slack)"
  "Pytest (stores-postgres)"
  "Pytest (stores-mysql)"
  "Pytest (codex-parity)"
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/3)"
  "E2E UI Tests (shard 1/3)"
  "E2E UI Tests (shard 2/3)"
  "UI Snapshot (visual baselines)"
  "Integration (openai-agents)"
)

ALLOW_SKIP=(
  "Docker build"
  "web test"
  "Pytest (runtime-harnesses)"
  "Pytest (runtime-policies)"
  "Pytest (runtime-core)"
  "Pytest (inner-rest)"
  "Pytest (tools)"
  "Pytest (repl-sdk)"
  "Pytest (server-approvals)"
  "Pytest (server-integration)"
  "Pytest (server-rest)"
  "Pytest (spec-llms)"
  "Pytest (integration-mock)"
  "Pytest (runner-app)"
  "Pytest (stores)"
  "Pytest (misc)"
  "Pytest (databricks)"
  "Pytest (slack)"
  "Pytest (stores-postgres)"
  "Pytest (stores-mysql)"
  "Pytest (codex-parity)"
  "E2E Tests (shard 0/4)"
  "E2E Tests (shard 1/4)"
  "E2E Tests (shard 2/4)"
  "E2E Tests (shard 3/4)"
  "E2E UI Tests (shard 0/3)"
  "E2E UI Tests (shard 1/3)"
  "E2E UI Tests (shard 2/3)"
  # Standalone main conditionally skips this job. The docs-routing aggregate
  # always emits it, and ALLOW_SKIP never masks an emitted failure.
  "UI Snapshot (visual baselines)"
  "Integration (openai-agents)"
)

is_allow_skip() { printf '%s\n' "${ALLOW_SKIP[@]}" | grep -qxF "$1"; }

# Maps every required check to its producer. evaluate-checks.sh uses producer
# pull-request runs, exact-attempt check IDs, and suite IDs so active or failed
# empty runs block while successful empty label-only suites may fall back.
workflow_for() {
  case "$1" in
    "Pre-commit checks" | "Version lockstep check") echo "Lint" ;;
    "Security Scan")        echo "Security Scan" ;;
    "Docker build")          echo "Docker build" ;;
    "web test")             echo "web Tests" ;;
    "Pytest ("*)             echo "CI" ;;
    "E2E Tests (shard "*)    echo "E2E Tests" ;;
    "E2E UI Tests (shard "*) echo "E2E UI Tests" ;;
    "UI Snapshot (visual baselines)") echo "UI Snapshot" ;;
    "Integration ("*)        echo "Integration Tests" ;;
    *)                       echo "" ;;
  esac
}
