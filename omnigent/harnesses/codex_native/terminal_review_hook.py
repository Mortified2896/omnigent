"""Schedule optional self-review when a native Codex turn becomes terminal.

This module wraps the bridge's existing atomic active-turn clear operation rather
than adding another lifecycle detector. The original clear result remains the
source of truth: stale/duplicate terminal edges never schedule review, and any
review failure is isolated from the user's task.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)
_review_tasks: set[asyncio.Task[bool]] = set()
_ORIGINAL_ATTR = "__omnigent_terminal_review_original__"
_HOOK_ATTR = "__omnigent_terminal_review_hook__"


def _reap_review_task(task: asyncio.Task[bool]) -> None:
    """Retain review tasks until completion and consume unexpected exceptions."""
    _review_tasks.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:  # noqa: BLE001 - this callback must never affect the main turn.
        _logger.warning("Codex self-review task reaping failed", exc_info=True)


def _schedule_review(
    *,
    bridge_dir: Path,
    session_id: str,
    parent_thread_id: str,
    primary_turn_id: str,
) -> None:
    """Schedule one non-blocking review on the current event loop when enabled."""
    try:
        from .self_review import review_completed_turn, self_review_enabled

        if not self_review_enabled():
            return
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Synchronous maintenance/test callers can legitimately clear bridge
        # state outside an event loop; evaluation is optional and must not
        # change their behavior.
        return
    except Exception:  # noqa: BLE001 - feature discovery is non-critical.
        _logger.warning("Could not initialize Codex self-review hook", exc_info=True)
        return

    task = loop.create_task(
        review_completed_turn(
            bridge_dir,
            session_id=session_id,
            parent_thread_id=parent_thread_id,
            primary_turn_id=primary_turn_id,
        ),
        name=f"codex-self-review:{primary_turn_id}",
    )
    _review_tasks.add(task)
    task.add_done_callback(_reap_review_task)


def install_self_review_hook() -> None:
    """Wrap ``bridge.clear_active_turn_id_if_matches`` exactly once."""
    from . import bridge

    current = bridge.clear_active_turn_id_if_matches
    if getattr(current, _HOOK_ATTR, False):
        return

    original: Callable[[Path, str | None], bool] = current

    def clear_and_review(bridge_dir: Path, completed_turn_id: str | None) -> bool:
        # Capture the exact parent identity before the original mutates state.
        # A scheduling decision is made only after the atomic clear succeeds.
        before = bridge.read_bridge_state(bridge_dir)
        result = original(bridge_dir, completed_turn_id)
        if (
            result
            and completed_turn_id is not None
            and before is not None
            and before.active_turn_id == completed_turn_id
        ):
            _schedule_review(
                bridge_dir=bridge_dir,
                session_id=before.session_id,
                parent_thread_id=before.thread_id,
                primary_turn_id=completed_turn_id,
            )
        return result

    setattr(clear_and_review, _HOOK_ATTR, True)
    setattr(clear_and_review, _ORIGINAL_ATTR, original)
    bridge.clear_active_turn_id_if_matches = clear_and_review


def uninstall_self_review_hook_for_tests() -> None:
    """Restore the unwrapped bridge clear helper for isolated tests."""
    from . import bridge

    current: Any = bridge.clear_active_turn_id_if_matches
    original = getattr(current, _ORIGINAL_ATTR, None)
    if callable(original):
        bridge.clear_active_turn_id_if_matches = original
