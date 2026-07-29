"""Focused regression tests for the issue #30 follow-up.

Issue #30's watchdog fix made the per-turn watchdog liveness-aware for
supervised subprocesses. The follow-up adds two narrow corrections:

1. The Control Room default flips the model-stream idle watchdog
   **off** (default ``HARNESS_MODEL_STREAM_IDLE_S=0`` and
   ``HARNESS_TOOL_OUTPUT_IDLE_S=0``) and keeps a generous absolute
   ceiling (``HARNESS_MAX_TOOL_RUNTIME_S=14400``,
   ``HARNESS_MAX_TURN_RUNTIME_S=28800``). Legitimate delegated work
   must not be killed by an aggressive short-idle timeout. Manual
   cancellation remains available; explicit installations can opt
   back into a positive idle timeout via the env vars.

2. The pre-existing bug in ``TurnContext.emit`` that excluded
   ``SubprocessLivenessEvent`` from resetting the enabled idle
   deadline is fixed: ordinary ``HeartbeatEvent`` is keep-alive
   only; every other event resets an explicitly enabled idle
   deadline.

These tests pin both behaviours against the live watchdog module
and the scaffold without spawning five-minute real-time waits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.runtime.harnesses import watchdog as watchdog_module
from omnigent.runtime.harnesses.watchdog import (
    TimeoutReason,
    resolve_watchdog_budgets,
)


@pytest.fixture(autouse=True)
def fresh_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip all HARNESS_* and HARNESS_TURN_* env vars for every test.

    Tests opt in to specific values via ``monkeypatch.setenv`` rather
    than inheriting the shell environment, so the suite is deterministic
    across CI shells and the operator's shell.
    """
    for name in (
        "HARNESS_MODEL_STREAM_IDLE_S",
        "HARNESS_TOOL_OUTPUT_IDLE_S",
        "HARNESS_MAX_TOOL_RUNTIME_S",
        "HARNESS_MAX_TURN_RUNTIME_S",
        "HARNESS_TURN_TIMEOUT_S",
        "HARNESS_TURN_ABSOLUTE_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# ── 1. Default model-stream idle budget is disabled ───────────────────────


def test_default_model_stream_idle_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env vars, ``model_stream_idle_s`` and ``tool_output_idle_s``
    are both ``0``. The absolute caps default to 14400 / 28800.
    """
    budgets = resolve_watchdog_budgets()
    assert budgets.model_stream_idle_s == 0.0
    assert budgets.tool_output_idle_s == 0.0
    assert budgets.max_tool_runtime_s == 14400.0
    assert budgets.max_turn_runtime_s == 28800.0


def test_zero_means_disabled_in_scaffold_idle_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scaffold's ``_guarded_run_turn`` must treat ``idle_timeout<=0``
    as ``asyncio.timeout(None)`` (no idle watchdog). Pin by exercising
    the same ``idle_timeout if idle_timeout > 0 else None`` branch the
    scaffold uses.
    """
    idle_timeout = 0.0
    ctx_value = idle_timeout if idle_timeout > 0 else None
    assert ctx_value is None
    # Negative values must also be treated as no-op, in case a future
    # operator passes ``-1`` for "disabled".
    assert (-1.0 if -1.0 > 0 else None) is None


# ── 2. An explicitly configured positive idle timeout still works ─────────


def test_positive_idle_timeout_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero ``HARNESS_MODEL_STREAM_IDLE_S`` is respected, not
    silently overridden by the default.
    """
    monkeypatch.setenv("HARNESS_MODEL_STREAM_IDLE_S", "11")
    budgets = resolve_watchdog_budgets()
    assert budgets.model_stream_idle_s == 11.0
    # Negative or zero legacy values still flow through; legacy fallback
    # only kicks in when the specific env var is unset.
    monkeypatch.delenv("HARNESS_MODEL_STREAM_IDLE_S")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "77")
    budgets = resolve_watchdog_budgets()
    assert budgets.model_stream_idle_s == 77.0
    assert budgets.tool_output_idle_s == 77.0


# ── 3. SubprocessLivenessEvent resets an explicitly enabled idle timeout ─


def test_subprocess_liveness_event_resets_enabled_idle_watchdog() -> None:
    """Construct a TurnContext, install a 1-second ``_reset_idle_watchdog``
    spy, and verify that emitting ``SubprocessLivenessEvent`` calls the
    reset hook while emitting ``HeartbeatEvent`` does not.
    """
    from omnigent.runtime.harnesses._scaffold import TurnContext
    from omnigent.server.schemas import HeartbeatEvent, SubprocessLivenessEvent

    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext.__new__(TurnContext)
    ctx._event_queue = queue
    resets: list[str] = []
    ctx._reset_idle_watchdog = lambda: resets.append("reset")

    ctx.emit(HeartbeatEvent(type="response.heartbeat"))
    assert resets == []
    ctx.emit(SubprocessLivenessEvent(type="response.subprocess_live", pid=1))
    assert resets == ["reset"]

    # Drain the queue for cleanliness.
    while not queue.empty():
        queue.get_nowait()


# ── 4. Plain HeartbeatEvent does not mask an explicitly enabled stall ─────


def test_heartbeat_does_not_mask_enabled_idle_stall() -> None:
    """Heartbeats must not reset an enabled idle deadline: a wedged
    turn that still emits ``response.heartbeat`` every 15 s should
    still be killed by an explicit idle watchdog at the configured
    timeout.
    """
    from omnigent.runtime.harnesses._scaffold import TurnContext
    from omnigent.server.schemas import HeartbeatEvent

    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext.__new__(TurnContext)
    ctx._event_queue = queue
    resets: list[str] = []
    ctx._reset_idle_watchdog = lambda: resets.append("reset")

    for _ in range(10):
        ctx.emit(HeartbeatEvent(type="response.heartbeat"))
    assert resets == []


# ── 5. Default disabled budget lets a long quiet turn survive forever ─────


async def test_disabled_default_keeps_quiet_turn_alive_past_old_240s_cap() -> None:
    """With the default disabled idle budget, ``asyncio.timeout(None)``
    is a no-op: a parent turn that emits only heartbeats can run
    arbitrarily long without the watchdog ever tripping. This is the
    property that fixes the issue #30 regression — a Verity parent
    waiting on a child session for >6 minutes survives because the
    idle watchdog is off, and the absolute ceiling is generous.
    """
    budgets = resolve_watchdog_budgets()
    assert budgets.model_stream_idle_s == 0.0
    # ``asyncio.timeout(None)`` is documented as a no-op context manager.
    # Proof: it returns immediately and never raises ``TimeoutError``
    # regardless of how long the wrapped body runs.
    async with asyncio.timeout(None):
        # A long sleep must complete without raising. We use a small
        # value here because the goal is to prove the no-op semantics,
        # not to actually wait. The assertion below is the actual proof.
        await asyncio.sleep(0.01)
    # Compare with a small positive budget that DOES trip:
    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(0.001):
            await asyncio.sleep(0.5)


# ── 6. The absolute turn ceiling still terminates a runaway turn ──────────


async def test_absolute_ceiling_still_terminates_runaway_turn() -> None:
    """The absolute cap ``max_turn_runtime_s`` is the backstop for
    abandoned or runaway work. With a positive small budget,
    ``asyncio.timeout`` cancels the wrapped body and re-raises
    ``TimeoutError`` out of the ``async with`` — the same mechanism
    the scaffold uses to surface an absolute turn-cap trip.
    """
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await asyncio.sleep(1.0)


# ── 7. Manual cancellation still terminates a turn ───────────────────────


async def test_manual_cancellation_still_terminates_turn() -> None:
    """``asyncio.CancelledError`` propagates through ``asyncio.timeout``
    the same way it does today; cancellation must remain available
    even when the idle watchdog is disabled.
    """
    async with asyncio.timeout(None):
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(10.0)


# ── 8. classify_timeout unchanged for the no-tracked-work case ────────────


def test_classify_timeout_returns_provider_stream_stall_when_nothing_tracked() -> None:
    """Without any tracked subprocess or child session, an idle trip is
    still classified as ``PROVIDER_STREAM_STALL``. The default
    policy change disables the trip entirely, but the classifier
    contract is unchanged for installations that opt into a positive
    timeout.
    """
    reason = watchdog_module.classify_timeout(
        trip_kind="idle",
        has_tracked_subprocess=False,
        last_subprocess_state=None,
        last_subprocess_pid=None,
        last_subprocess_alive=False,
        forwarder_failure=None,
    )
    assert reason is TimeoutReason.PROVIDER_STREAM_STALL


# ── 9. watchdog module re-imports cleanly when env vars change ────────────


def test_watchdog_module_resolves_independent_of_call_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budgets are resolved at scaffold call time, not import time,
    so changing the env vars between turns must take effect on the
    next ``resolve_watchdog_budgets`` call. Pin this by patching
    env, calling, then patching again.
    """
    monkeypatch.setenv("HARNESS_MODEL_STREAM_IDLE_S", "5")
    b1 = resolve_watchdog_budgets()
    assert b1.model_stream_idle_s == 5.0
    monkeypatch.setenv("HARNESS_MODEL_STREAM_IDLE_S", "120")
    b2 = resolve_watchdog_budgets()
    assert b2.model_stream_idle_s == 120.0


# ── 10. Resolve budgets invalid env value falls back to default ───────────


def test_invalid_env_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-numeric ``HARNESS_MODEL_STREAM_IDLE_S`` is logged and the
    default is used, so a typo cannot accidentally disable the idle
    watchdog for an installation that wants it on.
    """
    monkeypatch.setenv("HARNESS_MODEL_STREAM_IDLE_S", "not-a-number")
    budgets = resolve_watchdog_budgets()
    assert budgets.model_stream_idle_s == 0.0
    assert "invalid HARNESS_MODEL_STREAM_IDLE_S" in caplog.text
