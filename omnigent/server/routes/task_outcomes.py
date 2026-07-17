"""Task-outcome routes: list / get / submit review / unreviewed outcomes.

The HTTP surface for the task-outcome tracking vertical slice. All
endpoints sit under ``/v1`` (mounted by ``server/app.py``) and
follow the existing session-auth conventions:

- Reads: ``GET /v1/sessions/{id}/task-runs``,
  ``GET /v1/sessions/{id}/unreviewed-task-outcomes``, and
  ``GET /v1/task-runs/{task_run_id}`` all require
  ``LEVEL_READ`` on the parent conversation. The last one looks
  up the run, derives its conversation_id, and checks access
  against that.

- Writes: ``POST /v1/task-runs/{task_run_id}/review`` requires
  ``LEVEL_EDIT`` so a session collaborator can submit / correct
  their review (matches the comments-store pattern).

The router factory mirrors
:func:`omnigent.server.routes.comments.create_comments_router`:
``store`` is closed over; ``auth_provider`` + ``permission_store``
+ ``conversation_store`` are passed for the access helpers. The
reviewer's email is recorded as ``created_by`` (or ``None`` for
single-user mode, mapped via :func:`attribution_user`).

Submission model
----------------
The review endpoint is idempotent on ``(task_run_id, created_by)``:
a re-submit UPDATEs the existing row instead of appending a
duplicate. The store layer handles the SELECT-then-INSERT-or-UPDATE
race; the API just accepts the body and returns the row.

Skipping
--------
``verdict='skipped'`` is a real persisted state — distinct from
"no review yet". The API treats the two as separate: a missing
review renders as "Outcome not reviewed"; a skipped review
renders as "Skipped" (still re-editable later). Both are
reviewable via the same endpoint.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from omnigent.entities import (
    EVALUATOR_ACCURACY_VALUES,
    FAILURE_ATTRIBUTION_VALUES,
    REASONING_EFFORT_VALUES,
    REVIEW_ACTIONS,
    REVIEW_VERDICTS,
    ROUTE_FIT_VALUES,
    TASK_FAMILIES,
    MessageData,
    TaskRun,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import (
    attribution_user,
    get_user_id,
    require_access,
    require_user,
)
from omnigent.server.task_outcome_recorder import (
    get_recorder,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.task_outcome_store import (
    TaskOutcomeStore,
    UpsertTaskReviewInput,
)


def _serialise_run(run: TaskRun) -> dict[str, Any]:
    """Render a :class:`TaskRun` as an API JSON dict.

    Stable field names matching the field names in
    :class:`omnigent.entities.task_outcome.TaskRun`. The
    ``changed_files`` field comes from the entity's decoded list
    (the store reads the JSON column); the API serialises it as
    a list (omitted when ``None`` to keep the response compact).
    """
    return {
        "id": run.id,
        "conversation_id": run.conversation_id,
        "response_id": run.response_id,
        "triggering_message_id": run.triggering_message_id,
        "project_path": run.project_path,
        "task_description": run.task_description,
        "proposed_task_family": run.proposed_task_family,
        "estimated_difficulty": run.estimated_difficulty,
        "harness_id": run.harness_id,
        "requested_route_id": run.requested_route_id,
        "actual_provider": run.actual_provider,
        "actual_provider_model": run.actual_provider_model,
        "actual_provenance_verified": run.actual_provenance_verified,
        "execution_status": run.execution_status,
        "execution_started_at": run.execution_started_at,
        "execution_finished_at": run.execution_finished_at,
        "execution_duration_ms": run.execution_duration_ms,
        "selected_provider": run.selected_provider,
        "selected_model": run.selected_model,
        "reasoning_effort": run.reasoning_effort,
        "permission_mode": run.permission_mode,
        "omniroute_decision_id": run.omniroute_decision_id,
        "selection_strategy": run.selection_strategy,
        "billing_class": run.billing_class,
        "fallback_used": run.fallback_used,
        "terminal_status": run.terminal_status,
        "started_at": run.started_at,
        "terminal_at": run.terminal_at,
        "duration_ms": run.duration_ms,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_cost_usd": run.total_cost_usd,
        "response_summary": run.response_summary,
        "changed_files": run.changed_files,
        "commit_sha": run.commit_sha,
        "failure_error_code": run.failure_error_code,
        "failure_error_message": run.failure_error_message,
        "langfuse_trace_id": run.langfuse_trace_id,
        "langfuse_observation_id": run.langfuse_observation_id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _serialise_model_call(call: Any) -> dict[str, Any]:
    """Serialize sanitized per-request runtime provenance in ordinal order."""
    return {
        "id": call.id,
        "task_run_id": call.task_run_id,
        "ordinal": call.ordinal,
        "correlation_id": call.correlation_id,
        "requested_provider": call.requested_provider,
        "requested_model": call.requested_model,
        "requested_reasoning": call.requested_reasoning,
        "effective_reasoning": call.effective_reasoning,
        "stream": call.stream,
        "selected_provider": call.selected_provider,
        "selected_model": call.selected_model,
        "omniroute_request_id": call.omniroute_request_id,
        "omniroute_decision_id": call.omniroute_decision_id,
        "fallback_used": call.fallback_used,
        "selection_strategy": call.selection_strategy,
        "billing_class": call.billing_class,
        "provenance_verified": call.provenance_verified,
        "request_status": call.request_status,
        "http_status": call.http_status,
        "failure_stage": call.failure_stage,
        "error_code": call.error_code,
        "error_message": call.error_message,
        "started_at": call.started_at,
        "first_response_at": call.first_response_at,
        "finished_at": call.finished_at,
        "duration_ms": call.duration_ms,
    }


def _serialise_evaluation(evaluation: Any | None) -> dict[str, Any] | None:
    """Render a :class:`TaskEvaluation` as an API JSON dict, or ``None``."""
    if evaluation is None:
        return None
    return {
        "id": evaluation.id,
        "task_run_id": evaluation.task_run_id,
        "evaluator_type": evaluation.evaluator_type,
        "evaluator_provider": evaluation.evaluator_provider,
        "evaluator_model": evaluation.evaluator_model,
        "evaluator_route_id": evaluation.evaluator_route_id,
        "verdict": evaluation.verdict,
        "confidence": evaluation.confidence,
        "quality_score": evaluation.quality_score,
        "proposed_task_family": evaluation.proposed_task_family,
        "reasoning": evaluation.reasoning,
        "evidence": evaluation.evidence,
        "unresolved_issues": evaluation.unresolved_issues,
        "created_at": evaluation.created_at,
    }


def _serialise_review(review: Any | None) -> dict[str, Any] | None:
    """Render a :class:`TaskReview` as an API JSON dict, or ``None``."""
    if review is None:
        return None
    return {
        "id": review.id,
        "task_run_id": review.task_run_id,
        "verdict": review.verdict,
        "quality_score": review.quality_score,
        "final_task_family": review.final_task_family,
        "evaluator_accuracy": review.evaluator_accuracy,
        "comments": review.comments,
        "created_by": review.created_by,
        "review_action": review.review_action,
        "learning_eligible": review.learning_eligible,
        "route_fit": review.route_fit,
        "failure_attribution": review.failure_attribution,
        "preferred_route_id": review.preferred_route_id,
        "preferred_reasoning_effort": review.preferred_reasoning_effort,
        "source_evaluation_id": review.source_evaluation_id,
        "review_schema_version": review.review_schema_version,
        "created_at": review.created_at,
        "updated_at": review.updated_at,
    }


def _run_to_summary_dict(run: TaskRun) -> dict[str, Any]:
    """Lighter-weight summary used by the listing endpoint.

    Mirrors ``_serialise_run`` but drops the larger unbounded
    text fields (``task_description``, ``response_summary``,
    ``failure_error_message``) so a session with many runs
    doesn't ship their full bodies through the listing. The
    detail endpoint serves the full shape.
    """
    return {
        "id": run.id,
        "conversation_id": run.conversation_id,
        "response_id": run.response_id,
        "triggering_message_id": run.triggering_message_id,
        "terminal_status": run.terminal_status,
        "started_at": run.started_at,
        "terminal_at": run.terminal_at,
        "duration_ms": run.duration_ms,
        "selected_provider": run.selected_provider,
        "selected_model": run.selected_model,
        "requested_route_id": run.requested_route_id,
        "actual_provider": run.actual_provider,
        "actual_provider_model": run.actual_provider_model,
        "actual_provenance_verified": run.actual_provenance_verified,
        "fallback_used": run.fallback_used,
        "harness_id": run.harness_id,
        "proposed_task_family": run.proposed_task_family,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_cost_usd": run.total_cost_usd,
        "commit_sha": run.commit_sha,
        "changed_files_count": (len(run.changed_files) if run.changed_files else None),
        "failure_error_code": run.failure_error_code,
        "langfuse_trace_id": run.langfuse_trace_id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


# ── Pydantic bodies ──────────────────────────────────────────────────────


class UpsertReviewRequest(BaseModel):
    """Body of ``POST /v1/task-runs/{id}/review``.

    All fields except ``task_run_id`` (in the path) and
    ``verdict`` are optional; ``verdict`` is required so the
    operator must commit to at least ``skipped`` /
    ``unsure`` if they're not ready to grade. ``verdict`` is
    bounded by :data:`omnigent.entities.REVIEW_VERDICTS`.

    ``evaluator_accuracy`` is the human's view of how accurate
    the LLM verdict was (one of
    :data:`omnigent.entities.EVALUATOR_ACCURACY_VALUES`).
    ``final_task_family`` is the corrected family when the
    operator disagrees with the LLM's proposal.

    :param verdict: Required; bounded by ``REVIEW_VERDICTS``.
    :param quality_score: 1..5 (optional).
    :param final_task_family: One of ``TASK_FAMILIES`` (optional).
    :param evaluator_accuracy: One of ``EVALUATOR_ACCURACY_VALUES``
        (optional).
    :param comments: Free-text (optional).
    """

    model_config = ConfigDict(extra="forbid")

    action: str | None = None
    source_evaluation_id: str | None = Field(default=None, max_length=64)
    verdict: str | None = None
    quality_score: int | None = Field(default=None, ge=1, le=5)
    route_fit: str | None = None
    failure_attribution: str | None = None
    preferred_route_id: str | None = Field(default=None, max_length=64)
    preferred_reasoning_effort: str | None = None
    final_task_family: str | None = None
    evaluator_accuracy: str | None = None
    comments: str | None = Field(default=None, max_length=4000)


# ── Router factory ───────────────────────────────────────────────────────


async def _hydrate_missing_triggering_message_ids(
    runs: list[TaskRun],
    session_id: str,
    conversation_store: ConversationStore | None,
) -> list[dict[str, Any]]:
    """Bridge legacy runs whose recorder missed the user item race.

    Only hydrate when the session has exactly one user message and one
    unresolved run; otherwise the association remains fail-closed.
    """
    summaries = [_run_to_summary_dict(run) for run in runs]
    missing = [summary for summary in summaries if summary["triggering_message_id"] is None]
    if not missing or len(missing) != 1 or conversation_store is None:
        return summaries
    items = await asyncio.to_thread(
        conversation_store.list_items,
        session_id,
        limit=200,
        order="asc",
    )
    user_ids = [
        item.id
        for item in items.data
        if item.type == "message"
        and isinstance(item.data, MessageData)
        and item.data.role == "user"
        and not item.data.is_meta
    ]
    if len(user_ids) != 1:
        return summaries
    missing[0]["triggering_message_id"] = user_ids[0]
    return summaries


def create_task_outcomes_router(
    store: TaskOutcomeStore,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the task-outcomes router.

    All routes are scoped to ``/v1`` and follow the existing
    session-auth conventions: reads require ``LEVEL_READ``,
    writes require ``LEVEL_EDIT``. When ``permission_store`` is
    ``None``, all access is allowed (single-user mode).

    :param store: The :class:`TaskOutcomeStore` instance.
    :param conversation_store: Conversation store used by the
        access helpers (must be provided when ``permission_store``
        is non-None).
    :param auth_provider: Auth provider for user identity.
    :param permission_store: Permission store for session access
        checks. ``None`` disables permission enforcement.
    :returns: A configured :class:`APIRouter`.
    """
    if permission_store is not None and conversation_store is None:
        raise ValueError("conversation_store is required when permission_store is provided")

    router = APIRouter()

    # ── GET /sessions/{id}/task-runs ───────────────────────────────

    @router.get("/sessions/{session_id}/task-runs")
    async def list_session_task_runs(
        request: Request,
        session_id: str,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """List task runs for a session, newest-first.

        :returns: ``{"runs": [...]}`` with each entry in the
            summary shape (lighter than the detail endpoint).
        """
        user_id = get_user_id(request, auth_provider)
        await require_access(
            user_id,
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        runs = await _call_store(
            store.list_runs_for_conversation,
            conversation_id=session_id,
            limit=limit,
        )
        return {
            "object": "list",
            "runs": await _hydrate_missing_triggering_message_ids(
                runs, session_id, conversation_store
            ),
        }

    # ── GET /sessions/{id}/unreviewed-task-outcomes ─────────────────

    @router.get("/sessions/{session_id}/unreviewed-task-outcomes")
    async def list_unreviewed_task_outcomes(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        """Return terminal runs that have no review row.

        Filters the SQL ``list_unreviewed_runs`` query to the
        requested session. The UI uses this to render the
        "Outcome not reviewed" badges + the review-card list.
        """
        user_id = get_user_id(request, auth_provider)
        await require_access(
            user_id,
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        runs = await _call_store(
            store.list_unreviewed_runs,
            conversation_id=session_id,
            limit=limit,
        )
        return {
            "object": "list",
            "task_run_ids": [r.id for r in runs],
            "runs": [_run_to_summary_dict(r) for r in runs],
        }

    # ── GET /task-runs/by-response/{response_id} ───────────────────

    @router.get("/sessions/{session_id}/task-runs/by-response/{response_id}")
    async def get_task_run_for_response(
        request: Request, session_id: str, response_id: str
    ) -> dict[str, Any]:
        user_id = get_user_id(request, auth_provider)
        await require_access(user_id, session_id, LEVEL_READ, permission_store, conversation_store)
        run = await _call_store(
            store.get_run_for_response, response_id=response_id, conversation_id=session_id
        )
        if run is None:
            raise OmnigentError("Task run not found", code=ErrorCode.NOT_FOUND)
        detail = await _call_store(store.get_run_detail, task_run_id=run.id)
        run_payload = _serialise_run(detail.run)
        if run_payload["triggering_message_id"] is None:
            hydrated = await _hydrate_missing_triggering_message_ids(
                [detail.run], session_id, conversation_store
            )
            run_payload["triggering_message_id"] = hydrated[0]["triggering_message_id"]
        return {
            "run": run_payload,
            "model_calls": [
                _serialise_model_call(call)
                for call in await _call_store(store.list_model_calls, task_run_id=run.id)
            ],
            "evaluation": _serialise_evaluation(detail.evaluation),
            "review": _serialise_review(
                await _call_store(
                    store.get_review_for_run,
                    task_run_id=run.id,
                    created_by=attribution_user(user_id),
                )
            ),
            "any_review": _serialise_review(detail.review),
            "langfuse_pending": detail.langfuse_pending,
        }

    # ── GET /task-runs/{id} ─────────────────────────────────────────

    @router.get("/task-runs/{task_run_id}")
    async def get_task_run(
        request: Request,
        task_run_id: str,
    ) -> dict[str, Any]:
        """Return the full :class:`TaskRunDetail` aggregate.

        Looks up the run, derives its conversation_id, then
        enforces ``LEVEL_READ`` on that conversation. When the
        run doesn't exist OR the caller can't see the owning
        session, returns 404 (so we don't leak run existence
        across sessions).
        """
        user_id = get_user_id(request, auth_provider)
        run = await _call_store(store.get_run, task_run_id=task_run_id)
        if run is None:
            raise OmnigentError("Task run not found", code=ErrorCode.NOT_FOUND)
        await require_access(
            user_id,
            run.conversation_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        detail = await _call_store(store.get_run_detail, task_run_id=task_run_id)
        assert detail is not None  # row exists — we just fetched it
        review_for_caller = await _call_store(
            store.get_review_for_run,
            task_run_id=task_run_id,
            created_by=attribution_user(user_id),
        )
        return {
            "run": _serialise_run(detail.run),
            "model_calls": [
                _serialise_model_call(call)
                for call in await _call_store(store.list_model_calls, task_run_id=task_run_id)
            ],
            "evaluation": _serialise_evaluation(detail.evaluation),
            "review": _serialise_review(review_for_caller),
            "any_review": _serialise_review(detail.review),
            "langfuse_pending": detail.langfuse_pending,
        }

    # ── POST /task-runs/{id}/review ─────────────────────────────────

    @router.post("/task-runs/{task_run_id}/review")
    async def submit_task_run_review(
        request: Request,
        task_run_id: str,
        body: UpsertReviewRequest,
    ) -> dict[str, Any]:
        """Upsert a human review for a task run.

        Idempotent on ``(task_run_id, created_by)`` — re-submitting
        replaces the existing row. The endpoint requires
        ``LEVEL_EDIT`` on the run's owning session so a
        session collaborator can write a review but a read-only
        viewer cannot.

        After upserting, enqueues the Langfuse sync events for
        the new review so the next worker tick posts them.
        Skipped silently when Langfuse is unconfigured — the
        audit ``status='skipped'`` row was written at terminal
        time.
        """
        user_id = require_user(request, auth_provider)
        run = await _call_store(store.get_run, task_run_id=task_run_id)
        if run is None:
            raise OmnigentError("Task run not found", code=ErrorCode.NOT_FOUND)
        await require_access(
            user_id,
            run.conversation_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        legacy_submission = body.action is None and body.source_evaluation_id is None
        action = {"accept": "accepted", "adjust": "adjusted", "decline": "declined"}.get(
            body.action or "",
            body.action or ("declined" if body.verdict == "skipped" else "accepted"),
        )
        if action not in REVIEW_ACTIONS:
            raise OmnigentError(
                f"action must be one of {list(REVIEW_ACTIONS)!r}", code=ErrorCode.INVALID_INPUT
            )
        if action == "declined":
            verdict = "skipped"
        else:
            verdict = body.verdict
        if (action != "accepted" or legacy_submission) and (
            verdict not in REVIEW_VERDICTS or (action != "declined" and verdict == "skipped")
        ):
            raise OmnigentError(
                f"verdict must be one of {list(REVIEW_VERDICTS)!r}, got {verdict!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        evaluation = await _call_store(store.get_evaluation_for_run, task_run_id=task_run_id)
        if body.source_evaluation_id is not None and (
            evaluation is None or body.source_evaluation_id != evaluation.id
        ):
            raise HTTPException(status_code=409, detail="source evaluation is stale")
        if action == "accepted" and evaluation is not None and not legacy_submission:
            # Accept is deliberately resolved from the immutable evaluation.
            verdict = evaluation.verdict if evaluation.verdict != "inconclusive" else "unsure"
            quality_score = evaluation.quality_score
            final_task_family = evaluation.proposed_task_family
        elif action == "accepted" and not legacy_submission:
            raise OmnigentError(
                "an evaluation is required to accept", code=ErrorCode.INVALID_INPUT
            )
        else:
            quality_score = None if action == "declined" else body.quality_score
            final_task_family = None if action == "declined" else body.final_task_family
        if body.final_task_family is not None and body.final_task_family not in TASK_FAMILIES:
            raise OmnigentError(
                f"final_task_family must be one of {list(TASK_FAMILIES)!r}, "
                f"got {body.final_task_family!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        if body.route_fit is not None and body.route_fit not in ROUTE_FIT_VALUES:
            raise OmnigentError("invalid route_fit", code=ErrorCode.INVALID_INPUT)
        if (
            body.failure_attribution is not None
            and body.failure_attribution not in FAILURE_ATTRIBUTION_VALUES
        ):
            raise OmnigentError("invalid failure_attribution", code=ErrorCode.INVALID_INPUT)
        if (
            body.preferred_reasoning_effort is not None
            and body.preferred_reasoning_effort not in REASONING_EFFORT_VALUES
        ):
            raise OmnigentError("invalid preferred_reasoning_effort", code=ErrorCode.INVALID_INPUT)
        if body.preferred_route_id is not None and len(body.preferred_route_id) > 64:
            raise OmnigentError("preferred_route_id is too long", code=ErrorCode.INVALID_INPUT)
        if (
            body.evaluator_accuracy is not None
            and body.evaluator_accuracy not in EVALUATOR_ACCURACY_VALUES
        ):
            raise OmnigentError(
                f"evaluator_accuracy must be one of "
                f"{list(EVALUATOR_ACCURACY_VALUES)!r}, "
                f"got {body.evaluator_accuracy!r}",
                code=ErrorCode.INVALID_INPUT,
            )

        review = await _call_store(
            store.upsert_review,
            data=UpsertTaskReviewInput(
                task_run_id=task_run_id,
                verdict=verdict,
                created_by=attribution_user(user_id),
                quality_score=quality_score,
                final_task_family=final_task_family,
                evaluator_accuracy=None if action == "declined" else body.evaluator_accuracy,
                comments=body.comments,
                review_action=action,
                learning_eligible=action in ("accepted", "adjusted"),
                route_fit=None if action == "declined" else body.route_fit,
                failure_attribution=None if action == "declined" else body.failure_attribution,
                preferred_route_id=None if action == "declined" else body.preferred_route_id,
                preferred_reasoning_effort=None
                if action == "declined"
                else body.preferred_reasoning_effort,
                source_evaluation_id=body.source_evaluation_id
                or (evaluation.id if action == "accepted" and evaluation else None),
            ),
        )
        recorder = get_recorder()
        if recorder is not None:
            try:
                recorder.enqueue_langfuse_review(
                    task_run_id=task_run_id,
                    task_run=run,
                    review=review,
                )
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "task_outcomes: langfuse enqueue failed for review=%s",
                    review.id,
                    exc_info=True,
                )
        return _serialise_review(review)

    return router


# ── Helpers ──────────────────────────────────────────────────────────────


async def _call_store(fn: Any, /, **kwargs: Any) -> Any:
    """Invoke a synchronous store method on a worker thread.

    The task-outcome store is synchronous (mirroring the comment
    store convention). All route handlers run inside FastAPI's
    async context; calling the store directly would block the
    event loop, so we dispatch through ``asyncio.to_thread``.

    :param fn: A store method (e.g. ``store.get_run``).
    :param kwargs: Forwarded kwargs.
    :returns: Whatever *fn* returns.
    :raises HTTPException: When the store raises
        :class:`sqlalchemy.exc.SQLAlchemyError` (mapped to 500).
    """
    import asyncio

    try:
        return await asyncio.to_thread(fn, **kwargs)
    except SQLAlchemyError as exc:
        _logger.exception("task_outcomes: store call failed: %s", exc)
        raise HTTPException(status_code=500, detail="task outcome store error") from exc


_logger: Any = __import__("logging").getLogger(__name__)


__all__ = [
    "UpsertReviewRequest",
    "create_task_outcomes_router",
]
