"""Graph-traversal invariants for the v1 legal-transition graph.

These tests treat ``LEGAL_TRANSITIONS`` as a directed graph and assert
structural properties (reachability, terminal invariants, happy-path
legality) rather than enumerating individual pairs. They complement the
pairwise checks in ``test_transitions.py``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from itertools import pairwise

import pytest

from omnigent.autopilot_v1.states import (
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    OversightAutopilotState,
)
from omnigent.autopilot_v1.transitions import LEGAL_TRANSITIONS, legal_next_states

ALL_STATES: frozenset[OversightAutopilotState] = frozenset(OversightAutopilotState)


def _shortest_paths(
    start: OversightAutopilotState,
    graph: dict[OversightAutopilotState, frozenset[OversightAutopilotState]],
) -> dict[OversightAutopilotState, list[OversightAutopilotState]]:
    """Return a BFS shortest-path tree from ``start`` over ``graph``.

    Each reachable node maps to the list of states along one shortest
    path from ``start`` to that node (inclusive of both endpoints). When
    multiple shortest paths exist, any one is acceptable; the caller
    should not depend on a specific tie-break.
    """
    parents: dict[OversightAutopilotState, OversightAutopilotState | None] = {start: None}
    queue: deque[OversightAutopilotState] = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in graph.get(node, frozenset()):
            if nxt not in parents:
                parents[nxt] = node
                queue.append(nxt)
    paths: dict[OversightAutopilotState, list[OversightAutopilotState]] = {}
    for target, parent in parents.items():
        if parent is None:
            paths[target] = [target]
            continue
        path: list[OversightAutopilotState] = [target]
        current: OversightAutopilotState | None = parent
        while current is not None:
            path.append(current)
            current = parents[current]
        path.reverse()
        paths[target] = path
    return paths


def _reachable_from(
    start: OversightAutopilotState,
    graph: dict[OversightAutopilotState, frozenset[OversightAutopilotState]],
) -> set[OversightAutopilotState]:
    """Return the set of states reachable from ``start`` over ``graph``."""
    seen: set[OversightAutopilotState] = {start}
    queue: deque[OversightAutopilotState] = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in graph.get(node, frozenset()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


# ── Reachability from QUEUED ─────────────────────────────────────────────


def test_all_states_reachable_from_queued() -> None:
    reachable = _reachable_from(OversightAutopilotState.QUEUED, LEGAL_TRANSITIONS)
    missing = ALL_STATES - reachable
    assert not missing, f"States unreachable from queued: {sorted(s.value for s in missing)}"
    assert reachable == set(ALL_STATES)


def test_publishing_reachable_from_queued() -> None:
    paths = _shortest_paths(OversightAutopilotState.QUEUED, LEGAL_TRANSITIONS)
    assert OversightAutopilotState.PUBLISHING in paths, "PUBLISHING must be reachable from QUEUED"


def test_pr_ready_only_reachable_via_publishing() -> None:
    """Every shortest legal path from QUEUED to PR_READY must include PUBLISHING.

    Under the corrected graph, REVIEWING no longer transitions directly to
    PR_READY, so any path into PR_READY from QUEUED must traverse the
    PUBLISHING publication-prep step.
    """
    paths = _shortest_paths(OversightAutopilotState.QUEUED, LEGAL_TRANSITIONS)
    assert OversightAutopilotState.PR_READY in paths, "PR_READY must be reachable from QUEUED"
    pr_ready_paths: Iterable[list[OversightAutopilotState]] = [
        paths[OversightAutopilotState.PR_READY]
    ]
    # BFS yields exactly one shortest path per target (the first one found),
    # so a single path check is sufficient; iterate defensively in case the
    # implementation is ever extended to enumerate all shortest paths.
    for path in pr_ready_paths:
        assert OversightAutopilotState.PUBLISHING in path, (
            f"Expected every shortest QUEUED -> PR_READY path to traverse "
            f"PUBLISHING, but got: {[s.value for s in path]}"
        )


# ── Terminal states ──────────────────────────────────────────────────────


def test_terminal_states_have_no_outbound() -> None:
    for state in TERMINAL_STATES:
        assert legal_next_states(state) == frozenset(), (
            f"Terminal state {state.value!r} must have empty outbound set; "
            f"got {sorted(s.value for s in legal_next_states(state))}"
        )


def test_failed_and_done_have_no_outbound() -> None:
    # Explicit per-state assertion (in addition to the parametrized loop
    # above) so a failure names the offending terminal state.
    assert legal_next_states(OversightAutopilotState.FAILED) == frozenset()
    assert legal_next_states(OversightAutopilotState.DONE) == frozenset()


# ── Single-source invariants ─────────────────────────────────────────────


def test_no_legal_transition_reviewing_to_pr_ready() -> None:
    """REVIEWING -> PR_READY must not appear in the legal graph.

    This is the central invariant of the issue #17 fix: the publication
    step (PUBLISHING) is required between REVIEWING and PR_READY.
    """
    assert (
        OversightAutopilotState.PR_READY
        not in LEGAL_TRANSITIONS[OversightAutopilotState.REVIEWING]
    ), "REVIEWING -> PR_READY must be removed from the legal graph"


def test_no_legal_transition_enters_publishing_except_from_reviewing() -> None:
    """PUBLISHING is reachable ONLY from REVIEWING.

    This strengthens the correction: PUBLISHING has exactly one inbound
    edge in the legal graph, coming from REVIEWING.
    """
    inbound: list[OversightAutopilotState] = sorted(
        state
        for state, targets in LEGAL_TRANSITIONS.items()
        if OversightAutopilotState.PUBLISHING in targets
    )
    assert inbound == [OversightAutopilotState.REVIEWING], (
        f"PUBLISHING must be entered only from REVIEWING; "
        f"unexpected inbound edges from: {[s.value for s in inbound]}"
    )


def test_publishing_outbound_preserved() -> None:
    """The issue #17 fix must preserve PUBLISHING's existing outbound edges."""
    outbound = sorted(legal_next_states(OversightAutopilotState.PUBLISHING))
    assert outbound == sorted(
        [
            OversightAutopilotState.PR_READY,
            OversightAutopilotState.BLOCKED,
            OversightAutopilotState.FAILED,
        ]
    )


# ── Happy path ──────────────────────────────────────────────────────────


HAPPY_PATH: list[OversightAutopilotState] = [
    OversightAutopilotState.QUEUED,
    OversightAutopilotState.CLAIMED,
    OversightAutopilotState.PLANNING,
    OversightAutopilotState.IMPLEMENTING,
    OversightAutopilotState.TESTING,
    OversightAutopilotState.REVIEWING,
    OversightAutopilotState.PUBLISHING,
    OversightAutopilotState.PR_READY,
    OversightAutopilotState.DONE,
]


def test_happy_path_legal() -> None:
    """The canonical happy path is a legal walk through the graph."""
    pairs: Iterator[tuple[OversightAutopilotState, OversightAutopilotState]] = pairwise(HAPPY_PATH)
    illegal = [(a.value, b.value) for a, b in pairs if b not in LEGAL_TRANSITIONS[a]]
    assert not illegal, (
        f"Happy path has illegal edges: {illegal}. Full path: {[s.value for s in HAPPY_PATH]}"
    )


def test_happy_path_is_a_shortest_path() -> None:
    """The happy path must be a shortest legal path from QUEUED to DONE."""
    paths = _shortest_paths(OversightAutopilotState.QUEUED, LEGAL_TRANSITIONS)
    assert paths[OversightAutopilotState.DONE] == HAPPY_PATH, (
        f"Expected shortest QUEUED -> DONE path to be the happy path; "
        f"got: {[s.value for s in paths[OversightAutopilotState.DONE]]}"
    )


# ── Non-terminal coverage ────────────────────────────────────────────────


@pytest.mark.parametrize("state", sorted(NON_TERMINAL_STATES, key=lambda s: s.value))
def test_every_non_terminal_state_reachable_from_queued(state: OversightAutopilotState) -> None:
    """Each non-terminal state has at least one legal path from QUEUED."""
    reachable = _reachable_from(OversightAutopilotState.QUEUED, LEGAL_TRANSITIONS)
    assert state in reachable, f"Non-terminal state {state.value!r} must be reachable from QUEUED"


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_every_terminal_state_reachable_from_queued(state: OversightAutopilotState) -> None:
    """Terminal states (FAILED, DONE) are still reachable from QUEUED."""
    reachable = _reachable_from(OversightAutopilotState.QUEUED, LEGAL_TRANSITIONS)
    assert state in reachable, f"Terminal state {state.value!r} must be reachable from QUEUED"
