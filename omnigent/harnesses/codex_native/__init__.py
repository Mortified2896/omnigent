"""Native Codex harness integration hooks."""

# Terminal ownership lives in ``forwarder.py``. Keeping package import free of
# monkey-patching ensures bridge clears used by stale-steer recovery cannot
# accidentally schedule a review for a turn that was not terminal.
