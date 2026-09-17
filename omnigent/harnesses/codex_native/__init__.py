"""Native Codex harness integration hooks."""

from .terminal_review_hook import install_self_review_hook

# Install before sibling modules import the bridge helper by name. The wrapper
# preserves the original function's return/exception behavior and schedules
# evaluation only after a matching turn is atomically cleared.
install_self_review_hook()
