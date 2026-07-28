"""Tests for the v1 legal-transition graph."""

from __future__ import annotations

import pytest

from omnigent.autopilot_v1.states import OversightAutopilotState
from omnigent.autopilot_v1.transitions import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    assert_legal_transition,
    is_legal_transition,
    legal_next_states,
)

# Canonical legal pairs from the v1 contract.
LEGAL_PAIRS: list[tuple[OversightAutopilotState, OversightAutopilotState]] = [
    (OversightAutopilotState.QUEUED, OversightAutopilotState.CLAIMED),
    (OversightAutopilotState.QUEUED, OversightAutopilotState.BLOCKED),
    (OversightAutopilotState.QUEUED, OversightAutopilotState.FAILED),
    (OversightAutopilotState.CLAIMED, OversightAutopilotState.PLANNING),
    (OversightAutopilotState.CLAIMED, OversightAutopilotState.BLOCKED),
    (OversightAutopilotState.CLAIMED, OversightAutopilotState.FAILED),
    (OversightAutopilotState.PLANNING, OversightAutopilotState.IMPLEMENTING),
    (OversightAutopilotState.PLANNING, OversightAutopilotState.BLOCKED),
    (OversightAutopilotState.PLANNING, OversightAutopilotState.FAILED),
    (OversightAutopilotState.IMPLEMENTING, OversightAutopilotState.TESTING),
    (OversightAutopilotState.IMPLEMENTING, OversightAutopilotState.BLOCKED),
    (OversightAutopilotState.IMPLEMENTING, OversightAutopilotState.FAILED),
    (OversightAutopilotState.TESTING, OversightAutopilotState.REVIEWING),
    (OversightAutopilotState.TESTING, OversightAutopilotState.FIXING),
    (OversightAutopilotState.TESTING, OversightAutopilotState.BLOCKED),
    (OversightAutopilotState.TESTING, OversightAutopilotState.FAILED),
    (OversightAutopilotState.REVIEWING, OversightAutopilotState.FIXING),
    (OversightAutopilotState.REVIEWING, OversightAutopilotState.PR_READY),
    (OversightAutopilotState.REVIEWING, OversightAutopilotState.BLOCKED),
    (OversightAutopilotState.REVIEWING, OversightAutopilotState.FAILED),
    (OversightAutopilotState.FIXING, OversightAutopilotState.IMPLEMENTING),
    (OversightAutopilotState.FIXING, OversightAutopilotState.BLOCKED),
    (OversightAutopilotState.FIXING, OversightAutopilotState.FAILED),
    (OversightAutopilotState.PUBLISHING, OversightAutopilotState.PR_READY),
    (OversightAutopilotState.PUBLISHING, OversightAutopilotState.BLOCKED),
    (OversightAutopilotState.PUBLISHING, OversightAutopilotState.FAILED),
    (OversightAutopilotState.PR_READY, OversightAutopilotState.DONE),
    (OversightAutopilotState.PR_READY, OversightAutopilotState.FAILED),
    (OversightAutopilotState.BLOCKED, OversightAutopilotState.CLAIMED),
    (OversightAutopilotState.BLOCKED, OversightAutopilotState.QUEUED),
    (OversightAutopilotState.BLOCKED, OversightAutopilotState.FAILED),
]


# ── Graph structure ──────────────────────────────────────────────────────


def test_legal_transitions_keys_match_all_states() -> None:
    assert set(LEGAL_TRANSITIONS.keys()) == set(OversightAutopilotState)


def test_failed_and_done_have_empty_outbound() -> None:
    assert legal_next_states(OversightAutopilotState.FAILED) == frozenset()
    assert legal_next_states(OversightAutopilotState.DONE) == frozenset()


def test_blocked_can_resume() -> None:
    next_states = legal_next_states(OversightAutopilotState.BLOCKED)
    assert OversightAutopilotState.CLAIMED in next_states
    assert OversightAutopilotState.QUEUED in next_states
    assert OversightAutopilotState.FAILED in next_states


# ── is_legal_transition ──────────────────────────────────────────────────


@pytest.mark.parametrize("current, next_state", LEGAL_PAIRS)
def test_legal_transitions_accepted(
    current: OversightAutopilotState, next_state: OversightAutopilotState
) -> None:
    assert is_legal_transition(current, next_state) is True


@pytest.mark.parametrize("current, next_state", LEGAL_PAIRS)
def test_assert_legal_transition_does_not_raise(
    current: OversightAutopilotState, next_state: OversightAutopilotState
) -> None:
    # Should not raise.
    assert_legal_transition(current, next_state)


def test_self_transitions_are_rejected() -> None:
    for state in OversightAutopilotState:
        assert is_legal_transition(state, state) is False


def test_illegal_transition_queued_to_done_rejected() -> None:
    assert (
        is_legal_transition(OversightAutopilotState.QUEUED, OversightAutopilotState.DONE) is False
    )


def test_illegal_transition_implementing_to_pr_ready_rejected() -> None:
    assert (
        is_legal_transition(
            OversightAutopilotState.IMPLEMENTING,
            OversightAutopilotState.PR_READY,
        )
        is False
    )


def test_illegal_transition_blocked_to_implementing_rejected() -> None:
    # BLOCKED can resume to CLAIMED or QUEUED but cannot skip ahead to
    # IMPLEMENTING — that requires going through CLAIMED -> PLANNING.
    assert (
        is_legal_transition(OversightAutopilotState.BLOCKED, OversightAutopilotState.IMPLEMENTING)
        is False
    )


def test_illegal_transition_from_terminal_rejected() -> None:
    assert (
        is_legal_transition(OversightAutopilotState.DONE, OversightAutopilotState.QUEUED) is False
    )
    assert (
        is_legal_transition(OversightAutopilotState.FAILED, OversightAutopilotState.QUEUED)
        is False
    )


# ── assert_legal_transition raises ───────────────────────────────────────


def test_assert_legal_transition_raises_illegal_transition_error() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        assert_legal_transition(OversightAutopilotState.QUEUED, OversightAutopilotState.DONE)
    assert excinfo.value.current is OversightAutopilotState.QUEUED
    assert excinfo.value.attempted is OversightAutopilotState.DONE


def test_illegal_transition_message_contains_diagnostics() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        assert_legal_transition(OversightAutopilotState.QUEUED, OversightAutopilotState.DONE)
    msg = str(excinfo.value)
    assert "queued" in msg
    assert "done" in msg
    assert "claimed" in msg  # legal-next should be present
    assert "blocked" in msg  # legal-next should be present
    assert "failed" in msg  # legal-next should be present
    assert "OVERSIGHT_AUTOPILOT_V1" in msg  # contract doc hint


def test_illegal_transition_from_terminal_message_states_terminal() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        assert_legal_transition(OversightAutopilotState.DONE, OversightAutopilotState.QUEUED)
    msg = str(excinfo.value)
    assert "terminal" in msg


def test_illegal_transition_from_blocked_message_states_blocked() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        assert_legal_transition(OversightAutopilotState.BLOCKED, OversightAutopilotState.PR_READY)
    msg = str(excinfo.value)
    assert "blocked" in msg
    assert "non-terminal" in msg


def test_illegal_transition_error_attributes() -> None:
    err = IllegalTransitionError(
        current=OversightAutopilotState.QUEUED,
        attempted=OversightAutopilotState.DONE,
        legal=frozenset({OversightAutopilotState.CLAIMED}),
    )
    assert err.current is OversightAutopilotState.QUEUED
    assert err.attempted is OversightAutopilotState.DONE
    assert OversightAutopilotState.CLAIMED in err.legal
    # StrEnum values are lowercase; the message renders them as-is.
    assert "claimed" in str(err)


def test_legal_next_states_returns_frozenset() -> None:
    result = legal_next_states(OversightAutopilotState.CLAIMED)
    assert isinstance(result, frozenset)
    assert OversightAutopilotState.PLANNING in result


# ── is_legal_transition is a pure predicate ──────────────────────────────


def test_is_legal_transition_does_not_raise_on_unknown_inputs() -> None:
    # Belt-and-braces: ensure the predicate returns False rather than
    # raising on any pair, even if a state were missing from the table.
    for current in OversightAutopilotState:
        for next_state in OversightAutopilotState:
            result = is_legal_transition(current, next_state)
            assert isinstance(result, bool)
