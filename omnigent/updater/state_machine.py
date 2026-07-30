"""State machine for the external updater (issue #38 §1).

The state machine is exhaustive and explicit. Rollback is a distinct
phase — not a generic failure — so monitoring can alarm separately
on ``rollback_failed``.

The transition table below is the single source of truth for what
the controller may do next. Any attempt to transition to a state
that is not in the table raises :class:`StateTransitionError`.

The machine also exposes a small helper
:func:`validate_transition` for tests and the recovery logic, which
checks a transition without applying it.
"""

from __future__ import annotations

from enum import Enum


class UpdatePhase(str, Enum):
    """All updater phases, including non-terminal and terminal."""

    QUEUED = "queued"
    VALIDATING = "validating"
    BUILDING = "building"
    DRAINING = "draining"
    REHEARSING_MIGRATION = "rehearsing_migration"
    BACKING_UP = "backing_up"
    PROMOTING = "promoting"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


# Transition table: ``STATE_TRANSITIONS[old] = {allowed next states}``.
# ``REJECTED`` is reachable only from ``QUEUED`` and ``VALIDATING`` so
# a request that started building cannot be silently dropped without
# a record of what happened.
STATE_TRANSITIONS: dict[UpdatePhase, frozenset[UpdatePhase]] = {
    UpdatePhase.QUEUED: frozenset({UpdatePhase.VALIDATING, UpdatePhase.REJECTED}),
    UpdatePhase.VALIDATING: frozenset(
        {
            UpdatePhase.BUILDING,
            UpdatePhase.REJECTED,
        }
    ),
    UpdatePhase.BUILDING: frozenset(
        {
            UpdatePhase.DRAINING,
            UpdatePhase.FAILED,
        }
    ),
    UpdatePhase.DRAINING: frozenset(
        {
            UpdatePhase.REHEARSING_MIGRATION,
            UpdatePhase.FAILED,
        }
    ),
    UpdatePhase.REHEARSING_MIGRATION: frozenset(
        {
            UpdatePhase.BACKING_UP,
            UpdatePhase.FAILED,
        }
    ),
    UpdatePhase.BACKING_UP: frozenset(
        {
            UpdatePhase.PROMOTING,
            UpdatePhase.FAILED,
        }
    ),
    UpdatePhase.PROMOTING: frozenset(
        {
            UpdatePhase.VERIFYING,
            UpdatePhase.FAILED,
        }
    ),
    UpdatePhase.VERIFYING: frozenset(
        {
            UpdatePhase.SUCCEEDED,
            UpdatePhase.ROLLING_BACK,
        }
    ),
    UpdatePhase.SUCCEEDED: frozenset(),
    UpdatePhase.REJECTED: frozenset(),
    UpdatePhase.FAILED: frozenset(),
    UpdatePhase.ROLLING_BACK: frozenset(
        {
            UpdatePhase.ROLLED_BACK,
            UpdatePhase.ROLLBACK_FAILED,
        }
    ),
    UpdatePhase.ROLLED_BACK: frozenset(),
    UpdatePhase.ROLLBACK_FAILED: frozenset(),
}


_TERMINAL_STATES: frozenset[UpdatePhase] = frozenset(
    {
        UpdatePhase.SUCCEEDED,
        UpdatePhase.REJECTED,
        UpdatePhase.FAILED,
        UpdatePhase.ROLLED_BACK,
        UpdatePhase.ROLLBACK_FAILED,
    }
)


def is_terminal(state: UpdatePhase | str) -> bool:
    """Return whether ``state`` is a terminal state.

    Accepts either an :class:`UpdatePhase` or the underlying string
    so callers reading ``UpdatePhase(value)`` from disk don't have
    to convert twice.
    """
    if isinstance(state, str):
        try:
            state = UpdatePhase(state)
        except ValueError:
            return False
    return state in _TERMINAL_STATES


class StateTransitionError(RuntimeError):
    """Raised when a state transition violates the transition table.

    The controller catches this and transitions to ``failed`` (or
    ``rollback_failed`` if the violation happens after cutover)
    rather than crashing — a controller that crashes mid-update is
    a bug, but a controller that records ``failed`` is the
    documented fallback path.
    """

    def __init__(self, *, old: UpdatePhase, new: UpdatePhase) -> None:
        super().__init__(f"illegal state transition from {old.value!r} to {new.value!r}")
        self.old = old
        self.new = new


def validate_transition(old: UpdatePhase, new: UpdatePhase) -> None:
    """Raise :class:`StateTransitionError` if ``old -> new`` is illegal."""
    if new not in STATE_TRANSITIONS[old]:
        raise StateTransitionError(old=old, new=new)


def assert_initial_state(state: UpdatePhase) -> None:
    """Ensure a freshly loaded request is in ``QUEUED``.

    A non-``QUEUED`` state on a brand-new request means either the
    request was already processed (in which case the caller should
    fetch the result instead) or the request file was tampered with.
    """
    if state != UpdatePhase.QUEUED:
        raise ValueError(f"a fresh request must be in 'queued' state; got {state.value!r}")
