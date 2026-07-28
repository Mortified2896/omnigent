"""Tests for the v1 state vocabulary and classification helpers."""

from __future__ import annotations

from omnigent.autopilot_v1.states import (
    BLOCKED_STATES,
    COMPLETION_STATES,
    HUMAN_INTERVENTION_STATES,
    NON_RETRYABLE_FAILURE_STATES,
    NON_TERMINAL_STATES,
    RETRYABLE_FAILURE_STATES,
    TERMINAL_STATES,
    OversightAutopilotState,
    is_blocked,
    is_completion,
    is_retryable_failure,
    is_terminal,
    requires_human_intervention,
)

ALL_STATES = set(OversightAutopilotState)


# ── Membership tests ─────────────────────────────────────────────────────


def test_all_states_listed_in_classifications() -> None:
    """Every state must appear in exactly one of TERMINAL/NON_TERMINAL."""
    assert TERMINAL_STATES | NON_TERMINAL_STATES == ALL_STATES
    assert TERMINAL_STATES.isdisjoint(NON_TERMINAL_STATES)


def test_terminal_states_are_done_and_failed() -> None:
    assert (
        frozenset({OversightAutopilotState.DONE, OversightAutopilotState.FAILED})
        == TERMINAL_STATES
    )


def test_blocked_states_contains_only_blocked() -> None:
    assert frozenset({OversightAutopilotState.BLOCKED}) == BLOCKED_STATES


def test_human_intervention_states_are_blocked_and_pr_ready() -> None:
    assert (
        frozenset({OversightAutopilotState.BLOCKED, OversightAutopilotState.PR_READY})
        == HUMAN_INTERVENTION_STATES
    )


def test_non_retryable_failure_states_are_failed() -> None:
    assert frozenset({OversightAutopilotState.FAILED}) == NON_RETRYABLE_FAILURE_STATES


def test_retryable_failure_states_empty_in_v1() -> None:
    assert frozenset() == RETRYABLE_FAILURE_STATES


def test_completion_states_are_pr_ready_and_done() -> None:
    assert (
        frozenset({OversightAutopilotState.PR_READY, OversightAutopilotState.DONE})
        == COMPLETION_STATES
    )


# ── Pure-function helpers ────────────────────────────────────────────────


def test_is_terminal_true_for_done_and_failed() -> None:
    assert is_terminal(OversightAutopilotState.DONE) is True
    assert is_terminal(OversightAutopilotState.FAILED) is True


def test_is_terminal_false_for_non_terminal_states() -> None:
    for state in NON_TERMINAL_STATES:
        assert is_terminal(state) is False, state


def test_is_blocked_only_for_blocked_state() -> None:
    assert is_blocked(OversightAutopilotState.BLOCKED) is True
    for state in ALL_STATES - {OversightAutopilotState.BLOCKED}:
        assert is_blocked(state) is False, state


def test_requires_human_intervention_true_for_blocked_and_pr_ready() -> None:
    assert requires_human_intervention(OversightAutopilotState.BLOCKED) is True
    assert requires_human_intervention(OversightAutopilotState.PR_READY) is True


def test_requires_human_intervention_false_for_others() -> None:
    neutral = ALL_STATES - HUMAN_INTERVENTION_STATES
    for state in neutral:
        assert requires_human_intervention(state) is False, state


def test_is_retryable_failure_false_everywhere_in_v1() -> None:
    for state in ALL_STATES:
        assert is_retryable_failure(state) is False, state


def test_is_completion_true_for_pr_ready_and_done() -> None:
    assert is_completion(OversightAutopilotState.PR_READY) is True
    assert is_completion(OversightAutopilotState.DONE) is True


def test_is_completion_false_for_in_progress_states() -> None:
    for state in ALL_STATES - COMPLETION_STATES:
        assert is_completion(state) is False, state
