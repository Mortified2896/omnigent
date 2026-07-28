"""Oversight Autopilot v1 — legal transition graph.

Pure data: a dict mapping each state to its frozenset of legal next
states, plus a small set of pure helpers. The graph is the contract;
any future controller (issue #22) must call :func:`assert_legal_transition`
before persisting a state change.
"""

from __future__ import annotations

from omnigent.autopilot_v1.states import (
    COMPLETION_STATES,
    HUMAN_INTERVENTION_STATES,
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    OversightAutopilotState,
    is_blocked,
    is_terminal,
)

# Legal (current -> {next}) graph for the v1 lifecycle. ``FAILED`` and
# ``DONE`` have no outbound edges; ``PUBLISHING`` exists so the worker
# can hand the branch off to the controller without racing on the push.
LEGAL_TRANSITIONS: dict[OversightAutopilotState, frozenset[OversightAutopilotState]] = {
    OversightAutopilotState.QUEUED: frozenset(
        {
            OversightAutopilotState.CLAIMED,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.CLAIMED: frozenset(
        {
            OversightAutopilotState.PLANNING,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.PLANNING: frozenset(
        {
            OversightAutopilotState.IMPLEMENTING,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.IMPLEMENTING: frozenset(
        {
            OversightAutopilotState.TESTING,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.TESTING: frozenset(
        {
            OversightAutopilotState.REVIEWING,
            OversightAutopilotState.FIXING,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.REVIEWING: frozenset(
        {
            OversightAutopilotState.FIXING,
            OversightAutopilotState.PR_READY,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.FIXING: frozenset(
        {
            OversightAutopilotState.IMPLEMENTING,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.PUBLISHING: frozenset(
        {
            OversightAutopilotState.PR_READY,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.PR_READY: frozenset(
        {
            OversightAutopilotState.DONE,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.BLOCKED: frozenset(
        {
            OversightAutopilotState.CLAIMED,
            OversightAutopilotState.QUEUED,
            OversightAutopilotState.FAILED,
        }
    ),
    OversightAutopilotState.FAILED: frozenset(),
    OversightAutopilotState.DONE: frozenset(),
}


class IllegalTransitionError(ValueError):
    """Raised when a proposed state transition is not in the legal graph."""

    def __init__(
        self,
        current: OversightAutopilotState,
        attempted: OversightAutopilotState,
        legal: frozenset[OversightAutopilotState],
    ) -> None:
        self.current = current
        self.attempted = attempted
        self.legal = legal
        super().__init__(self._render_message())

    def _classify(self, state: OversightAutopilotState) -> str:
        # Order matters: terminal check first so a terminal state is
        # described as terminal even if it is also a completion state.
        parts: list[str] = []
        if is_terminal(state):
            parts.append("terminal")
        else:
            parts.append("non-terminal")
        if is_blocked(state):
            parts.append("blocked")
        if state in HUMAN_INTERVENTION_STATES:
            parts.append("requires-human-intervention")
        if state in COMPLETION_STATES:
            parts.append("completion")
        return "|".join(parts) if parts else "unclassified"

    def _render_message(self) -> str:
        legal_list = (
            sorted(s.value for s in self.legal)
            if self.legal
            else ["<none — terminal state, no outbound transitions>"]
        )
        attempted_value = self.attempted.value
        current_value = self.current.value
        current_kind = self._classify(self.current)
        return (
            f"Illegal Oversight Autopilot v1 state transition: "
            f"current={current_value!r} ({current_kind}) "
            f"attempted={attempted_value!r}. "
            f"Legal next states from {current_value!r}: {legal_list}. "
            "Consult docs/OVERSIGHT_AUTOPILOT_V1.md (State machine, "
            "Legal transitions) before changing the controller."
        )

    def __str__(self) -> str:
        return self._render_message()


def is_legal_transition(
    current: OversightAutopilotState, next_state: OversightAutopilotState
) -> bool:
    """Return ``True`` iff ``current -> next_state`` is in the legal graph."""
    if current == next_state:
        # Self-transitions are not modeled; reject them so the controller
        # surfaces accidental no-ops instead of silently persisting them.
        return False
    return next_state in LEGAL_TRANSITIONS.get(current, frozenset())


def legal_next_states(
    current: OversightAutopilotState,
) -> frozenset[OversightAutopilotState]:
    """Return the legal outbound states from ``current``.

    Always returns a frozenset (empty for terminal states) so callers
    can treat the result as immutable.
    """
    return LEGAL_TRANSITIONS.get(current, frozenset())


def assert_legal_transition(
    current: OversightAutopilotState, next_state: OversightAutopilotState
) -> None:
    """Raise :class:`IllegalTransitionError` if the transition is not legal."""
    if not is_legal_transition(current, next_state):
        raise IllegalTransitionError(
            current=current,
            attempted=next_state,
            legal=legal_next_states(current),
        )


# ── Internal sanity assertions ────────────────────────────────────────────
# Catch typos at import time rather than at the first illegal transition.

_assert_all_states_covered: set[OversightAutopilotState] = set(LEGAL_TRANSITIONS.keys())
_known_states = set(OversightAutopilotState)
_missing = _known_states - _assert_all_states_covered
if _missing:
    raise RuntimeError(
        f"LEGAL_TRANSITIONS missing entries for states: {sorted(s.value for s in _missing)}"
    )

_assert_legal_targets_are_known: set[OversightAutopilotState] = set()
for _targets in LEGAL_TRANSITIONS.values():
    _assert_legal_targets_are_known.update(_targets)
_unknown_targets = _assert_legal_targets_are_known - _known_states
if _unknown_targets:
    raise RuntimeError(
        f"LEGAL_TRANSITIONS references unknown states: {sorted(s.value for s in _unknown_targets)}"
    )

_assert_terminal_states_have_no_outbound: set[OversightAutopilotState] = set()
for _state in TERMINAL_STATES:
    if LEGAL_TRANSITIONS.get(_state, frozenset()):
        _assert_terminal_states_have_no_outbound.add(_state)
if _assert_terminal_states_have_no_outbound:
    raise RuntimeError(
        f"Terminal states have outbound transitions in LEGAL_TRANSITIONS: "
        f"{sorted(s.value for s in _assert_terminal_states_have_no_outbound)}"
    )

_assert_non_terminal_states_have_outbound: set[OversightAutopilotState] = set()
for _state in NON_TERMINAL_STATES:
    if not LEGAL_TRANSITIONS.get(_state, frozenset()):
        _assert_non_terminal_states_have_outbound.add(_state)
if _assert_non_terminal_states_have_outbound:
    raise RuntimeError(
        f"Non-terminal states missing outbound transitions in LEGAL_TRANSITIONS: "
        f"{sorted(s.value for s in _assert_non_terminal_states_have_outbound)}"
    )

_assert_blocked_can_resume: frozenset[OversightAutopilotState] = LEGAL_TRANSITIONS.get(
    OversightAutopilotState.BLOCKED, frozenset()
)
if not _assert_blocked_can_resume:
    raise RuntimeError(
        "BLOCKED state has no outbound transitions; the contract requires "
        "BLOCKED to be resumable to CLAIMED or QUEUED on human clarification."
    )

_assert_failed_targets_only_terminal_or_back: frozenset[OversightAutopilotState] = (
    LEGAL_TRANSITIONS.get(OversightAutopilotState.FAILED, frozenset())
)
if _assert_failed_targets_only_terminal_or_back:
    raise RuntimeError("FAILED must be terminal; LEGAL_TRANSITIONS should give it an empty set.")

_assert_done_targets_only_terminal: frozenset[OversightAutopilotState] = LEGAL_TRANSITIONS.get(
    OversightAutopilotState.DONE, frozenset()
)
if _assert_done_targets_only_terminal:
    raise RuntimeError("DONE must be terminal; LEGAL_TRANSITIONS should give it an empty set.")

del (
    _assert_all_states_covered,
    _known_states,
    _missing,
    _assert_legal_targets_are_known,
    _unknown_targets,
    _assert_terminal_states_have_no_outbound,
    _assert_non_terminal_states_have_outbound,
    _assert_blocked_can_resume,
    _assert_failed_targets_only_terminal_or_back,
    _assert_done_targets_only_terminal,
)
