#!/bin/zsh
set -eu
source /Users/Jo/GitHub/_worktrees/omnigent/mac-tool-free-execution/diagnostics/tool-free/runtime-env.sh
unset OMNIGENT_O3_HARD_TOOL_FREE
unset OMNIGENT_O3_TOOL_FREE_QUALIFICATIONS
/Users/Jo/GitHub/omnigent-o3-routing-review-mvp/.venv/bin/omnigent server --port 6768 --background
/Users/Jo/GitHub/omnigent-o3-routing-review-mvp/.venv/bin/omnigent host --server http://127.0.0.1:6768 --background --non-interactive
