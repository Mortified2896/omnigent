"""Oversight Autopilot v1 — state vocabulary and classifications.

Pure in-memory types. No persistence, no I/O. The state machine is the
authoritative vocabulary used by the controller (future issue #22) and
the persistence layer (future issue #18).

Members of :class:`OversightAutopilotState` mirror the v1 contract.
Classification sets are kept as module-level frozensets so downstream
modules can perform O(1) membership tests without instantiating state.
"""

from __future__ import annotations

from enum import StrEnum


class OversightAutopilotState(StrEnum):
    """One state of an issue run through the Oversight Autopilot v1 pipeline.

    States spell out the lifecycle: the run is queued for intake, claimed
    by the controller, planned, implemented, tested, reviewed, fixed if
    needed, and finally published for human review. ``BLOCKED`` and
    ``FAILED`` are off-ramps; ``DONE`` is the terminal success state
    after human merge.
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    PUBLISHING = "publishing"
    PR_READY = "pr_ready"
    BLOCKED = "blocked"
    FAILED = "failed"
    DONE = "done"


# Terminal states — no further legal transitions out of these.
TERMINAL_STATES: frozenset[OversightAutopilotState] = frozenset(
    {OversightAutopilotState.DONE, OversightAutopilotState.FAILED}
)

# Non-terminal states — the run may still transition.
NON_TERMINAL_STATES: frozenset[OversightAutopilotState] = frozenset(
    set(OversightAutopilotState) - TERMINAL_STATES
)

# States where the run is paused waiting on something (usually a human
# clarification or a failing test that the controller routes back through
# the fix loop). ``BLOCKED`` itself is not in ``COMPLETION_STATES``; it is
# an intermediate pause, not an end state.
BLOCKED_STATES: frozenset[OversightAutopilotState] = frozenset({OversightAutopilotState.BLOCKED})

# States where the contract mandates human intervention to advance.
# ``BLOCKED`` needs a human clarification; ``PR_READY`` needs a human merge.
HUMAN_INTERVENTION_STATES: frozenset[OversightAutopilotState] = frozenset(
    {OversightAutopilotState.BLOCKED, OversightAutopilotState.PR_READY}
)

# States a future controller may transition back FROM to retry. In v1
# this is intentionally empty: retry happens by routing back into
# ``IMPLEMENTING`` from ``FIXING``/``REVIEWING``, not by re-entering
# these states themselves. Defined for forward compatibility.
RETRYABLE_FAILURE_STATES: frozenset[OversightAutopilotState] = frozenset()

# ``FAILED`` is the only non-retryable terminal state in v1.
NON_RETRYABLE_FAILURE_STATES: frozenset[OversightAutopilotState] = frozenset(
    {OversightAutopilotState.FAILED}
)

# Completion states: ``PR_READY`` awaits human merge; ``DONE`` is reached
# only after the controller confirms the merge landed.
COMPLETION_STATES: frozenset[OversightAutopilotState] = frozenset(
    {OversightAutopilotState.PR_READY, OversightAutopilotState.DONE}
)


def is_terminal(state: OversightAutopilotState) -> bool:
    """Return ``True`` if ``state`` is terminal (no legal outbound transitions)."""
    return state in TERMINAL_STATES


def is_blocked(state: OversightAutopilotState) -> bool:
    """Return ``True`` if ``state`` is a blocked pause state."""
    return state in BLOCKED_STATES


def requires_human_intervention(state: OversightAutopilotState) -> bool:
    """Return ``True`` if the contract requires a human to advance past ``state``."""
    return state in HUMAN_INTERVENTION_STATES


def is_retryable_failure(state: OversightAutopilotState) -> bool:
    """Return ``True`` if a future controller may retry from ``state``.

    v1 reserves this for forward compatibility; the set is currently empty.
    Use ``is_terminal(state) and state in NON_RETRYABLE_FAILURE_STATES``
    to test for non-retryable failure.
    """
    return state in RETRYABLE_FAILURE_STATES


def is_completion(state: OversightAutopilotState) -> bool:
    """Return ``True`` if ``state`` represents a run completion.

    ``PR_READY`` is included: it is the natural completion of the
    controller's work, even though a human merge is still pending.
    """
    return state in COMPLETION_STATES
