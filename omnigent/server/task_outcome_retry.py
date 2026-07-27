"""Durable automatic retry worker for deferred task-outcome evaluations."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress

from omnigent.server.task_outcome_evaluator import evaluator_retry_delays
from omnigent.server.task_outcome_recorder import TaskOutcomeRecorder

_logger = logging.getLogger(__name__)


def _positive_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.error("Invalid %s=%r; using %d", name, raw, default)
        return default
    return max(minimum, value)


async def run_evaluation_retry_worker(
    recorder: TaskOutcomeRecorder,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Recover interrupted claims and conservatively dispatch due M3 retries."""
    interval = _positive_int("OMNIGENT_EVALUATOR_RETRY_POLL_SECONDS", 60, 15)
    batch_size = _positive_int("OMNIGENT_EVALUATOR_RETRY_BATCH_SIZE", 2, 1)
    stale_seconds = _positive_int("OMNIGENT_EVALUATOR_PENDING_STALE_SECONDS", 900, 60)
    max_attempts = len(evaluator_retry_delays()) + 1
    stopper = stop_event or asyncio.Event()

    now = int(time.time())
    try:
        recovered = await asyncio.to_thread(
            recorder.store.recover_stale_pending_evaluations,
            now=now,
            stale_before=now,
        )
        if recovered:
            _logger.warning("Recovered %d interrupted outcome evaluator attempt(s)", recovered)
    except Exception:
        _logger.exception("Outcome evaluator retry startup recovery failed")

    while True:
        now = int(time.time())
        try:
            await asyncio.to_thread(
                recorder.store.recover_stale_pending_evaluations,
                now=now,
                stale_before=now - stale_seconds,
            )
            due = await asyncio.to_thread(
                recorder.store.claim_due_evaluations,
                now=now,
                max_attempts=max_attempts,
                limit=batch_size,
            )
            for run in due:
                recorder.dispatch_claimed_evaluation(run)
        except Exception:
            _logger.exception("Outcome evaluator retry worker tick failed")
        if stopper.is_set():
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(stopper.wait(), timeout=interval)


async def stop_evaluation_retry_worker(task: asyncio.Task[None]) -> None:
    """Cancel and join a retry worker during ASGI shutdown."""
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
