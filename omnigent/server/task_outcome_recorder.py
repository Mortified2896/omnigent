"""Task-outcome recorder: relays hook into the task-outcome tables.

Wires the existing response lifecycle events the relay loop already
processes (``response.in_progress`` / ``response.completed`` /
``response.failed`` / ``response.cancelled`` / ``response.incomplete``)
into the four task-outcome tables. Three call sites in the relay:

- **start (response.in_progress)** — INSERT a :class:`TaskRun` row
  with ``terminal_status='running'`` + the routing snapshot the
  approval flow staged in the process-local
  :data:`_routing_snapshot_for_session`. Also stamps ``response_id``
  on the new row (so the same row can be looked up by both
  ``task_run_id`` and ``response_id``).

- **terminal (response.completed/failed/cancelled/incomplete)** —
  UPDATE the row's status + duration, populate the usage bucket
  from ``response.completed.usage``, capture the
  ``response.failed.error.code`` / ``message`` /
  ``response.incomplete.incomplete_details.reason``, and enqueue
  the LLM evaluator via :func:`asyncio.create_task` so the relay
  loop returns immediately.

- **session lookup** — translate a harness-side
  ``response_id`` into a :class:`TaskRun` for the review-card
  UI. Used by the API layer.

The recorder is intentionally a thin module-level façade: it
doesn't own its own task loop, doesn't import the relay's local
state, and doesn't spawn any background work beyond the
evaluator :func:`asyncio.create_task` per terminal event.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omnigent.server.langfuse_sync import (
    build_root_observation_payload,
    build_score_payloads,
    langfuse_configured,
)
from omnigent.server.task_outcome_evaluator import evaluate_task_outcome
from omnigent.stores.task_outcome_store import (
    CreateTaskRunInput,
    EnqueueLangfuseEventInput,
    TaskOutcomeStore,
    UpdateTaskRunTerminalInput,
)

if TYPE_CHECKING:
    from omnigent.entities import Conversation

_logger = logging.getLogger(__name__)


# ── Routing snapshot staging ─────────────────────────────────────────────
#
# When the routing-agent approval flow accepts a proposal it
# updates the conversation row in `update_conversation(...)` —
# but the task-outcome recorder wants to snapshot the FULL proposal
# (selected_model, decision_id, evaluator_provenance, etc.) before
# the harness resolves anything concrete. The approval flow writes
# the proposal here keyed by session_id; the relay reads + clears
# the entry on the matching `response.in_progress`.
#
# This is process-local state: each AP instance has its own copy.
# For multi-instance deployments the snapshot would need to ride
# on the conversation row (which the migration already supports
# for the subset we read; the rest is currently staged here for
# the single-instance happy path that this vertical slice ships
# against).

_routing_snapshots_lock = threading.Lock()
_routing_snapshots: dict[str, RoutingSnapshot] = {}


@dataclass
class RoutingSnapshot:
    """A snapshot of the routing-agent proposal at task start.

    Captured when ``_await_route_approval`` accepts a proposal,
    consumed by the relay on ``response.in_progress``. The
    snapshot lives in a process-local dict (see
    :data:`_routing_snapshots`) so the API hot path doesn't
    re-read the conversation row.

    :param requested_route_id: Native OmniRoute route id.
    :param reasoning_effort: Routing snapshot.
    :param permission_mode: Routing snapshot.
    :param omniroute_decision_id: Per-call decision id from the
        OmniRoute header. ``None`` when the LLM path doesn't
        surface one.
    :param selection_strategy: Strategy reported by OmniRoute.
    :param billing_class: Billing class for the resolved model.
    :param fallback_used: ``True`` when the routing call fell
        back to a secondary model.
    :param proposed_harness: Harness name from the proposal
        (e.g. ``"OpenCode Native"``).
    :param estimated_difficulty: Optional difficulty hint.
    :param evaluator_route_id: The native route id used to call
        the routing agent itself (``router_evaluator_route`` in
        :class:`RouteProposal`).
    :param evaluator_model: Concrete model the routing agent
        called (e.g. ``"databricks/databricks-claude-haiku-4-5"``).
    :param evaluator_provider: Concrete provider.
    :param evaluator_billing_class: Billing class for the
        evaluator model.
    :param evaluator_fallback_used: ``True`` when the routing
        call itself fell back.
    :param evaluator_decision_id: Decision id for the
        routing-agent call.
    """

    routing_proposal_id: str | None = None
    routing_decision_id: str | None = None
    requested_route_id: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None
    omniroute_decision_id: str | None = None
    selection_strategy: str | None = None
    billing_class: str | None = None
    fallback_used: bool | None = None
    proposed_harness: str | None = None
    estimated_difficulty: str | None = None
    evaluator_route_id: str | None = None
    evaluator_model: str | None = None
    evaluator_provider: str | None = None
    evaluator_billing_class: str | None = None
    evaluator_fallback_used: bool | None = None
    evaluator_decision_id: str | None = None


def stage_routing_snapshot(session_id: str, snapshot: RoutingSnapshot) -> None:
    """Stage *snapshot* for *session_id* — consumed on next in_progress.

    Called from the routing-agent approval flow once the user
    accepts the proposal. Re-staging overwrites (so a user
    re-approving a fresh proposal updates the snapshot for the
    next in_progress).
    """
    with _routing_snapshots_lock:
        _routing_snapshots[session_id] = snapshot


def consume_routing_snapshot(session_id: str) -> RoutingSnapshot | None:
    """Return and clear the staged snapshot for *session_id*.

    Called from the relay on the first ``response.in_progress``
    after staging. Returns ``None`` when no snapshot was staged
    (e.g. route approval is OFF and the routing-agent path
    didn't run). The relay still creates a :class:`TaskRun` row
    in that case — the routing columns just stay ``NULL``.
    """
    with _routing_snapshots_lock:
        return _routing_snapshots.pop(session_id, None)


def discard_routing_snapshot(session_id: str) -> None:
    """Drop any staged approval metadata for *session_id*.

    A user can approve a proposal and then switch the session back to
    manual routing before the runner starts. Do not let that old proposal
    annotate the next direct task.
    """
    with _routing_snapshots_lock:
        _routing_snapshots.pop(session_id, None)


def peek_routing_snapshot(session_id: str) -> RoutingSnapshot | None:
    """Return the staged snapshot for *session_id* without clearing."""
    with _routing_snapshots_lock:
        return _routing_snapshots.get(session_id)


# ── Configurable test seam ───────────────────────────────────────────────
#
# Tests can swap the store / override the evaluator dispatch without
# monkey-patching. The relay looks up the recorder via the module-
# level ``get_recorder()`` so the harness-process can install a
# recorder pointing at a different store (no global state mutation
# in production paths).

_recorder_lock = threading.Lock()
_recorder: TaskOutcomeRecorder | None = None


def set_recorder(recorder: TaskOutcomeRecorder | None) -> None:
    """Install (or clear) the process-wide recorder.

    Called by the server's lifespan once the store is wired in.
    Tests use this to install a recorder pointing at a
    tmp-path SQLite file before exercising the relay paths.
    """
    global _recorder
    with _recorder_lock:
        _recorder = recorder


def get_recorder() -> TaskOutcomeRecorder | None:
    """Return the process-wide recorder (or ``None`` when unset)."""
    with _recorder_lock:
        return _recorder


# ── Recorder ──────────────────────────────────────────────────────────────


@dataclass
class TaskOutcomeRecorder:
    """Recorder state shared across the relay loop calls.

    A single instance is built at lifespan startup and held on the
    app's runtime state. The relay loop calls
    :meth:`on_response_in_progress` / :meth:`on_response_terminal`
    / :meth:`on_response_failed` per event. Stores the store
    reference + a reference to the conversation store (the latter
    is used for ``get_conversation`` to read the harness bound to
    the session).

    :param store: The :class:`TaskOutcomeStore`.
    """

    store: TaskOutcomeStore
    # Optional override for tests; production code uses the
    # default asyncio.create_task dispatch.
    _task_spawner: Any = field(
        default=lambda coro: asyncio.create_task(coro),
        repr=False,
    )

    # ── start: response.in_progress ───────────────────────────────────

    def on_response_in_progress(
        self,
        *,
        session_id: str,
        conversation: Conversation,
        response_id: str,
        model_id: str | None,
        user_message_id: str | None,
        user_message_summary: str | None,
        project_path: str | None,
    ) -> str | None:
        """Create the :class:`TaskRun` row for a new turn.

        Idempotent on ``(workspace_id, response_id)``: a second
        in_progress for the same ``response_id`` (which the
        harness doesn't normally emit, but defence-in-depth is
        cheap) returns the existing run id without writing a
        duplicate row.

        :returns: The new (or existing) task run id, or ``None``
            when no routing snapshot was staged and the recorder
            is in ``observe_only=False`` mode (every turn is
            tracked; ``None`` is only returned if the store
            write itself fails).
        """
        snapshot = consume_routing_snapshot(session_id)
        try:
            existing = self.store.get_run_for_response(response_id, session_id)
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None and isinstance(existing.id, str):
            return existing.id
        harness_id = (
            conversation.harness_override
            or (conversation.agent_id if conversation else None)
            or None
        )
        try:
            run = self.store.create_run(
                CreateTaskRunInput(
                    conversation_id=session_id,
                    response_id=response_id,
                    triggering_message_id=user_message_id,
                    project_path=project_path,
                    task_description=user_message_summary,
                    harness_id=harness_id,
                    requested_route_id=(snapshot.requested_route_id if snapshot else None),
                    # selected_* is the execution request (not the router
                    # evaluator). Actual provider/model are intentionally
                    # absent until execution-path metadata is observed.
                    selected_provider=_requested_execution_provider(model_id, snapshot),
                    selected_model=_requested_execution_model(model_id, snapshot),
                    reasoning_effort=(snapshot.reasoning_effort if snapshot else None),
                    permission_mode=(snapshot.permission_mode if snapshot else None),
                    omniroute_decision_id=(snapshot.omniroute_decision_id if snapshot else None),
                    routing_proposal_id=(snapshot.routing_proposal_id if snapshot else None),
                    routing_decision_id=(snapshot.routing_decision_id if snapshot else None),
                    selection_strategy=(snapshot.selection_strategy if snapshot else None),
                    billing_class=(snapshot.billing_class if snapshot else None),
                    fallback_used=(snapshot.fallback_used if snapshot is not None else None),
                )
            )
        except Exception as exc:  # noqa: BLE001  # never propagate
            _logger.warning(
                "task_outcome_recorder: failed to create run for session=%s response=%s: %s",
                session_id,
                response_id,
                exc,
            )
            return None
        # Stamp the run id back onto the relay-local state so the
        # terminal handler can find the same row. The relay passes
        # the same response_id to ``on_response_terminal`` so the
        # store could look it up directly; this avoids that lookup.
        return run.id

    # ── terminal ──────────────────────────────────────────────────────

    def on_response_terminal(
        self,
        *,
        task_run_id: str,
        terminal_status: str,
        terminal_at: int,
        response_summary: str | None = None,
        changed_files: list[str] | None = None,
        commit_sha: str | None = None,
        failure_error_code: str | None = None,
        failure_error_message: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_cost_usd: float | None = None,
        response_id: str | None = None,
        triggering_message_id: str | None = None,
    ) -> None:
        """Update a :class:`TaskRun` row with terminal data.

        Persists the row + enqueues the LLM evaluator + enqueues
        Langfuse sync events for the new terminal status. Never
        raises — every error is logged at WARNING.

        :param task_run_id: The :class:`TaskRun.id` from the
            matching ``on_response_in_progress`` call.
        :param terminal_status: ``completed`` / ``failed`` /
            ``cancelled`` / ``incomplete``.
        :param terminal_at: Unix epoch seconds.
        :param response_summary: Truncated final assistant
            response (sanitised by the relay).
        :param changed_files: List of changed file paths the
            harness surfaced (already sanitised).
        :param commit_sha: Git commit SHA when the harness surfaced it.
        :param failure_error_code: Error code from the terminal
            event (``response.failed.error.code`` or
            ``response.incomplete.incomplete_details.reason``).
        :param failure_error_message: Truncated error message.
        :param input_tokens / output_tokens / total_cost_usd:
            From ``response.completed.usage`` (best-effort).
        :param response_id: Set when in_progress landed AFTER
            the relay's first INSERT (rare; only if approval ran
            before the harness emitted in_progress and the relay
            created the run with ``response_id=None``).
        """
        try:
            updated = self.store.update_run_terminal(
                UpdateTaskRunTerminalInput(
                    task_run_id=task_run_id,
                    terminal_status=terminal_status,
                    terminal_at=terminal_at,
                    response_id=response_id,
                    triggering_message_id=triggering_message_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_cost_usd=total_cost_usd,
                    response_summary=response_summary,
                    changed_files=changed_files,
                    commit_sha=commit_sha,
                    failure_error_code=failure_error_code,
                    failure_error_message=failure_error_message,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "task_outcome_recorder: failed to terminalise run=%s: %s",
                task_run_id,
                exc,
            )
            return

        if updated is None:
            return

        # Enqueue the LLM evaluator (always — even on failure so
        # the operator has a chance to see "evaluator: inconclusive"
        # and review the failure).
        self._spawn_evaluator(updated)

        # Enqueue Langfuse sync. We do this synchronously so a
        # Langfuse outage is captured on the row + the worker
        # retries; the actual POST happens off-thread in the
        # worker.
        self._enqueue_langfuse_for_run(updated)

    # ── convenience: resolution helpers ────────────────────────────────

    @staticmethod
    def _spawn_evaluator(run: Any) -> None:
        """Spawn the LLM evaluator as a fire-and-forget task.

        Reads ``task_run_id`` + the current user-message summary
        (best-effort) from the run row. The evaluator never
        raises; on failure it persists ``verdict='inconclusive'``.
        """
        recorder = get_recorder()
        if recorder is None:
            return

        async def _evaluate() -> None:
            try:
                outcome = await evaluate_task_outcome(
                    recorder.store,
                    run,
                    triggering_message_summary=run.task_description,
                )
                # After the evaluator lands, re-enqueue Langfuse
                # so the llm-verdict score can ship too.
                recorder._enqueue_langfuse_for_evaluation(run, outcome.evaluation)
            except Exception as exc:  # noqa: BLE001  # never propagate
                _logger.warning(
                    "task_outcome_recorder: evaluator dispatch failed for run=%s: %s",
                    run.id,
                    exc,
                )

        try:
            recorder._task_spawner(_evaluate())
        except RuntimeError:
            # No event loop (e.g. tests that don't install one).
            _logger.debug(
                "task_outcome_recorder: skipping evaluator spawn (no event loop) for run=%s",
                run.id,
            )

    def _enqueue_langfuse_for_run(self, run: Any) -> None:
        """Enqueue Langfuse sync events for a terminalised :class:`TaskRun`.

        When Langfuse is unconfigured, write a single
        ``status='skipped'`` audit row instead of a real
        ``status='pending'`` row.

        Always writes at least the root observation payload; the
        evaluation-row is enqueued separately by
        :meth:`_enqueue_langfuse_for_evaluation` once the LLM
        evaluator completes.
        """
        if not langfuse_configured():
            try:
                self.store.mark_langfuse_skipped(run.id)
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "task_outcome_recorder: failed to mark langfuse skipped for run=%s",
                    run.id,
                    exc_info=True,
                )
            return
        try:
            root_payload = build_root_observation_payload(run)
            self.store.enqueue_langfuse_event(
                EnqueueLangfuseEventInput(
                    task_run_id=run.id,
                    event_type="task_root",
                    idempotency_key=root_payload["id"],
                    payload=root_payload,
                )
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "task_outcome_recorder: failed to enqueue langfuse root for run=%s",
                run.id,
                exc_info=True,
            )

    def _enqueue_langfuse_for_evaluation(self, run: Any, evaluation: Any) -> None:
        """Enqueue Langfuse score events for an :class:`TaskEvaluation`.

        Called from the evaluator spawn coroutine once an
        evaluation row has landed. Skipped silently when
        Langfuse is unconfigured — the audit ``status='skipped'``
        row was already written by
        :meth:`_enqueue_langfuse_for_run`.
        """
        if not langfuse_configured():
            return
        try:
            for score in build_score_payloads(run, evaluation, review=None):
                payload = {
                    "id": score.id,
                    "sessionId": score.session_id,
                    "traceId": score.trace_id,
                    "name": score.name,
                    "value": score.value,
                    "dataType": score.data_type,
                    "comment": score.comment,
                }
                if score.observation_id:
                    payload["observationId"] = score.observation_id
                self.store.enqueue_langfuse_event(
                    EnqueueLangfuseEventInput(
                        task_run_id=run.id,
                        task_evaluation_id=evaluation.id,
                        event_type=(
                            "llm_verdict"
                            if score.name == "task_verdict_llm"
                            else (
                                "llm_quality" if score.name == "task_quality_llm" else "llm_family"
                            )
                        ),
                        idempotency_key=score.id,
                        payload=payload,
                    )
                )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "task_outcome_recorder: failed to enqueue langfuse evaluation for run=%s",
                run.id,
                exc_info=True,
            )

    def enqueue_langfuse_review(
        self,
        *,
        task_run_id: str,
        task_run: Any,
        review: Any,
    ) -> None:
        """Enqueue Langfuse score events for a human review.

        Called from the API after ``upsert_review`` lands. When
        Langfuse is unconfigured the row is left as-is (the
        ``status='skipped'`` audit row was written at terminal
        time and there's no per-review audit row to add).
        """
        if not langfuse_configured():
            return
        try:
            for score in build_score_payloads(task_run, evaluation=None, review=review):
                payload = {
                    "id": score.id,
                    "sessionId": score.session_id,
                    "traceId": score.trace_id,
                    "name": score.name,
                    "value": score.value,
                    "dataType": score.data_type,
                    "comment": score.comment,
                }
                if score.observation_id:
                    payload["observationId"] = score.observation_id
                event_type = (
                    "human_verdict"
                    if score.name == "task_verdict_human"
                    else (
                        "human_quality"
                        if score.name == "task_quality_human"
                        else (
                            "human_family"
                            if score.name == "task_family_human"
                            else (
                                "human_route_fit"
                                if score.name == "route_fit_human"
                                else (
                                    "human_failure_attribution"
                                    if score.name == "failure_attribution_human"
                                    else (
                                        "human_learning_eligibility"
                                        if score.name == "learning_eligible"
                                        else "llm_evaluation_accuracy"
                                    )
                                )
                            )
                        )
                    )
                )
                self.store.enqueue_langfuse_event(
                    EnqueueLangfuseEventInput(
                        task_run_id=task_run_id,
                        event_type=event_type,
                        idempotency_key=score.id,
                        payload=payload,
                    )
                )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "task_outcome_recorder: failed to enqueue langfuse review for run=%s",
                task_run_id,
                exc_info=True,
            )


# ── Selection resolution helpers ────────────────────────────────────────


def _requested_execution_model(
    model_id: str | None, snapshot: RoutingSnapshot | None
) -> str | None:
    """Pick the model id to stamp onto the :class:`TaskRun` row.

    Order: (1) the model requested from the harness; (2) the approved
        OmniRoute combo from the routing snapshot; (3) ``None``.  The model
        that evaluated the route is deliberately never used here.

        :param model_id: Model id from the harness (``None`` when the
            harness didn't report one).
        :param snapshot: The staged :class:`RoutingSnapshot` (``None``
            when route approval was off).
        :returns: The model id to stamp, or ``None``.
    """
    if isinstance(model_id, str) and model_id.strip():
        return model_id.strip()
    if snapshot is not None and snapshot.requested_route_id:
        return snapshot.requested_route_id
    return None


def _requested_execution_provider(
    model_id: str | None, snapshot: RoutingSnapshot | None
) -> str | None:
    """Pick the provider id to stamp onto the :class:`TaskRun` row.

    Order: (1) OmniRoute for an approved combo; (2) provider inferred from
        the harness request; (3) ``None``.  Routing-evaluator provenance is
        intentionally excluded.

        :param model_id: Model id from the harness.
        :param snapshot: The staged :class:`RoutingSnapshot` (``None``
            when route approval was off).
        :returns: The provider id, or ``None``.
    """
    if snapshot is not None and snapshot.requested_route_id:
        return "omniroute"
    if isinstance(model_id, str) and "/" in model_id:
        return model_id.split("/", 1)[0]
    return None


# ── Convenience for tests / diagnostics ──────────────────────────────────


def summarise_run_for_log(task_run: Any) -> dict[str, Any]:
    """Render a :class:`TaskRun` as a flat dict for ``_logger.info``.

    Used by the relay's "task run terminal" log line. Mirrors the
    existing log-shape convention so log scrapers don't have to
    special-case this module.

    :param task_run: The :class:`TaskRun` row.
    :returns: A JSON-ready dict.
    """
    return {
        "task_run_id": task_run.id,
        "conversation_id": task_run.conversation_id,
        "response_id": task_run.response_id,
        "terminal_status": task_run.terminal_status,
        "selected_provider": task_run.selected_provider,
        "selected_model": task_run.selected_model,
        "requested_route_id": task_run.requested_route_id,
        "duration_ms": task_run.duration_ms,
        "fallback_used": task_run.fallback_used,
    }


__all__ = [
    "RoutingSnapshot",
    "TaskOutcomeRecorder",
    "consume_routing_snapshot",
    "discard_routing_snapshot",
    "get_recorder",
    "peek_routing_snapshot",
    "set_recorder",
    "stage_routing_snapshot",
    "summarise_run_for_log",
]
