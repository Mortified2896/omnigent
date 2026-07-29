"""State-machine validity tests (issue #38 §1)."""

from __future__ import annotations

import itertools

import pytest

from omnigent.updater.protocol import TERMINAL_STATES
from omnigent.updater.state_machine import (
    STATE_TRANSITIONS,
    StateTransitionError,
    UpdatePhase,
    assert_initial_state,
    is_terminal,
    validate_transition,
)


def test_terminal_phases_are_explicit() -> None:
    """Every terminal phase must be in TERMINAL_STATES (and vice versa)."""
    expected_terminal = {
        UpdatePhase.SUCCEEDED,
        UpdatePhase.REJECTED,
        UpdatePhase.FAILED,
        UpdatePhase.ROLLED_BACK,
        UpdatePhase.ROLLBACK_FAILED,
    }
    assert set(TERMINAL_STATES) == expected_terminal
    for phase in expected_terminal:
        assert is_terminal(phase)
        assert is_terminal(phase.value)


def test_terminal_phases_have_no_successors() -> None:
    """Terminal phases cannot transition further."""
    for phase in (
        UpdatePhase.SUCCEEDED,
        UpdatePhase.REJECTED,
        UpdatePhase.FAILED,
        UpdatePhase.ROLLED_BACK,
        UpdatePhase.ROLLBACK_FAILED,
    ):
        assert STATE_TRANSITIONS[phase] == frozenset()


def test_only_reachable_terminal_from_validating_is_rejected() -> None:
    """A validation failure must transition to ``rejected`` — not a failure."""
    assert STATE_TRANSITIONS[UpdatePhase.VALIDATING] == frozenset(
        {UpdatePhase.BUILDING, UpdatePhase.REJECTED}
    )


def test_rollback_distinct_from_failure() -> None:
    """A failed verification must transition to ``rolling_back``, not ``failed``."""
    assert UpdatePhase.ROLLING_BACK in STATE_TRANSITIONS[UpdatePhase.VERIFYING]
    assert UpdatePhase.FAILED not in STATE_TRANSITIONS[UpdatePhase.VERIFYING]


def test_illegal_transition_raises() -> None:
    """Cross-cutting transitions are explicitly forbidden."""
    with pytest.raises(StateTransitionError) as exc:
        validate_transition(UpdatePhase.QUEUED, UpdatePhase.PROMOTING)
    assert "illegal state transition" in str(exc.value)
    assert exc.value.old == UpdatePhase.QUEUED
    assert exc.value.new == UpdatePhase.PROMOTING


def test_happy_path_walks_to_succeeded() -> None:
    """The legal sequence reaches ``succeeded`` without skipping phases."""
    chain = [
        UpdatePhase.QUEUED,
        UpdatePhase.VALIDATING,
        UpdatePhase.BUILDING,
        UpdatePhase.DRAINING,
        UpdatePhase.REHEARSING_MIGRATION,
        UpdatePhase.BACKING_UP,
        UpdatePhase.PROMOTING,
        UpdatePhase.VERIFYING,
        UpdatePhase.SUCCEEDED,
    ]
    for prev, nxt in itertools.pairwise(chain):
        validate_transition(prev, nxt)


def test_assert_initial_state_rejects_resumed_requests() -> None:
    """A request file already past ``queued`` is not a fresh request."""
    with pytest.raises(ValueError):
        assert_initial_state(UpdatePhase.BUILDING)


def test_state_machine_uses_string_values() -> None:
    """The phase values are short snake_case strings (used in JSON + filenames)."""
    for phase in UpdatePhase:
        assert "_" not in phase.value or phase.value.islower()
        assert "," not in phase.value
