"""SQLAlchemy-backed implementation of the task-outcome store.

Mirrors the structure of
:mod:`omnigent.stores.comment_store.sqlalchemy_store`: a single
:class:`TaskOutcomeStore` backed by the four ORM tables in
:mod:`omnigent.db.db_models` (``SqlTaskRun`` / ``SqlTaskEvaluation``
/ ``SqlTaskReview`` / ``SqlLangfuseSyncOutbox``). All writes are
wrapped in a managed session that auto-commits on success and
auto-rolls back on failure.

The cross-table ``get_run_detail`` aggregate is implemented as a
single fan-out of three small queries (run → evaluation, run →
review for created_by, run → outbox count). Each query hits the
index that supports it (``ix_task_evaluations_run``,
``ix_task_reviews_run`` / ``uq_task_reviews_run_reviewer``,
``ix_langfuse_outbox_run``) so the whole aggregate is O(1) round
trips regardless of how many other runs/reviews exist.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import func, select

from omnigent.db.db_models import (
    SqlLangfuseSyncOutbox,
    SqlTaskEvaluation,
    SqlTaskReview,
    SqlTaskRun,
    current_workspace_id,
)
from omnigent.db.enum_codecs import (
    decode_langfuse_outbox_status,
    decode_task_evaluation_type,
    decode_task_run_status,
    encode_langfuse_outbox_status,
    encode_task_evaluation_type,
    encode_task_run_status,
)
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch
from omnigent.entities import (
    LangfuseOutboxRow,
    TaskEvaluation,
    TaskReview,
    TaskRun,
    decode_json_list,
    encode_json_list,
)
from omnigent.stores.task_outcome_store import (
    CreateTaskEvaluationInput,
    CreateTaskRunInput,
    EnqueueLangfuseEventInput,
    TaskOutcomeStore,
    TaskRunDetail,
    UpdateTaskRunTerminalInput,
    UpsertTaskReviewInput,
)


def _generate_run_id() -> str:
    """Mint a fresh ``task_runs.id`` UUID with the ``tr_`` prefix.

    Using a separate prefix from ``conversation_items`` so the
    :class:`TaskRun` id can travel alongside response ids without
    ambiguity — a reviewer scanning a URL can tell which kind of
    id they're looking at from the prefix alone.
    """
    return f"tr_{uuid.uuid4().hex}"


def _generate_evaluation_id() -> str:
    """Mint a fresh ``task_evaluations.id`` UUID with the ``tev_`` prefix."""
    return f"tev_{uuid.uuid4().hex}"


def _generate_review_id() -> str:
    """Mint a fresh ``task_reviews.id`` UUID with the ``trv_`` prefix."""
    return f"trv_{uuid.uuid4().hex}"


def _generate_outbox_id() -> str:
    """Mint a fresh ``langfuse_sync_outbox.id`` UUID with the ``lfs_`` prefix."""
    return f"lfs_{uuid.uuid4().hex}"


def _run_row_to_entity(row: SqlTaskRun) -> TaskRun:
    """Convert a :class:`SqlTaskRun` ORM row to a :class:`TaskRun`."""
    return TaskRun(
        id=row.id,
        conversation_id=row.conversation_id,
        terminal_status=decode_task_run_status(row.terminal_status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        execution_status=row.execution_status,
        evaluation_status=row.evaluation_status,
        execution_started_at=row.execution_started_at,
        execution_finished_at=row.execution_finished_at,
        execution_duration_ms=row.execution_duration_ms,
        evaluation_started_at=row.evaluation_started_at,
        evaluation_finished_at=row.evaluation_finished_at,
        timeout_type=row.timeout_type,
        last_useful_activity_at=row.last_useful_activity_at,
        actual_provider=row.actual_provider,
        actual_provider_model=row.actual_provider_model,
        actual_provenance_verified=row.actual_provenance_verified,
        response_id=row.response_id,
        triggering_message_id=row.triggering_message_id,
        project_path=row.project_path,
        task_description=row.task_description,
        proposed_task_family=row.proposed_task_family,
        estimated_difficulty=row.estimated_difficulty,
        harness_id=row.harness_id,
        requested_route_id=row.requested_route_id,
        selected_provider=row.selected_provider,
        selected_model=row.selected_model,
        reasoning_effort=row.reasoning_effort,
        permission_mode=row.permission_mode,
        omniroute_decision_id=row.omniroute_decision_id,
        selection_strategy=row.selection_strategy,
        billing_class=row.billing_class,
        fallback_used=row.fallback_used,
        started_at=row.started_at,
        terminal_at=row.terminal_at,
        duration_ms=row.duration_ms,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        total_cost_usd=row.total_cost_usd,
        response_summary=row.response_summary,
        changed_files=decode_json_list(row.changed_files_json),
        commit_sha=row.commit_sha,
        failure_error_code=row.failure_error_code,
        failure_error_message=row.failure_error_message,
        langfuse_trace_id=row.langfuse_trace_id,
        langfuse_observation_id=row.langfuse_observation_id,
    )


def _evaluation_row_to_entity(row: SqlTaskEvaluation) -> TaskEvaluation:
    """Convert a :class:`SqlTaskEvaluation` ORM row to a :class:`TaskEvaluation`."""
    return TaskEvaluation(
        id=row.id,
        task_run_id=row.task_run_id,
        evaluator_type=decode_task_evaluation_type(row.evaluator_type),
        verdict=row.verdict,
        created_at=row.created_at,
        evaluator_provider=row.evaluator_provider,
        evaluator_model=row.evaluator_model,
        evaluator_route_id=row.evaluator_route_id,
        confidence=row.confidence,
        quality_score=row.quality_score,
        proposed_task_family=row.proposed_task_family,
        reasoning=row.reasoning,
        evidence=decode_json_list(row.evidence_json),
        unresolved_issues=decode_json_list(row.unresolved_issues_json),
    )


def _review_row_to_entity(row: SqlTaskReview) -> TaskReview:
    """Convert a :class:`SqlTaskReview` ORM row to a :class:`TaskReview`."""
    return TaskReview(
        id=row.id,
        task_run_id=row.task_run_id,
        verdict=row.verdict,
        created_at=row.created_at,
        updated_at=row.updated_at,
        quality_score=row.quality_score,
        final_task_family=row.final_task_family,
        evaluator_accuracy=row.evaluator_accuracy,
        comments=row.comments,
        created_by=row.created_by,
        review_action=row.review_action,
        learning_eligible=row.learning_eligible,
        route_fit=row.route_fit,
        failure_attribution=row.failure_attribution,
        preferred_route_id=row.preferred_route_id,
        preferred_reasoning_effort=row.preferred_reasoning_effort,
        source_evaluation_id=row.source_evaluation_id,
        review_schema_version=row.review_schema_version,
    )


def _outbox_row_to_entity(row: SqlLangfuseSyncOutbox) -> LangfuseOutboxRow:
    """Convert a :class:`SqlLangfuseSyncOutbox` row to a :class:`LangfuseOutboxRow`."""
    try:
        payload = json.loads(row.payload_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Defensive: an outbox row should never carry a non-JSON
        # payload, but if a future migration drops a row in
        # mid-flight we don't want the API to crash. Surface an
        # opaque marker the worker can recognise.
        payload = {"_corrupt": True, "_raw_bytes": len(row.payload_json)}
    return LangfuseOutboxRow(
        id=row.id,
        task_run_id=row.task_run_id,
        event_type=row.event_type,
        idempotency_key=row.idempotency_key,
        payload=payload,
        status=decode_langfuse_outbox_status(row.status),
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        task_evaluation_id=row.task_evaluation_id,
        last_error=row.last_error,
        delivered_at=row.delivered_at,
    )


class SqlAlchemyTaskOutcomeStore(TaskOutcomeStore):
    """SQLAlchemy-backed :class:`TaskOutcomeStore`.

    Persists all four tables in a single relational database via
    SQLAlchemy ORM. Writes are wrapped in a managed session that
    auto-commits on success and auto-rolls back on failure.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the SQLAlchemy task-outcome store.

        :param storage_location: SQLAlchemy database URI, e.g.
            ``"sqlite:///omnigent.db"`` or
            ``"postgresql+psycopg://user:pass@host/db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    # ── task_runs ─────────────────────────────────────────────────────

    def create_run(self, data: CreateTaskRunInput) -> TaskRun:
        """INSERT a fresh ``task_runs`` row. See base class for contract."""
        now = now_epoch()
        run_id = _generate_run_id()
        row = SqlTaskRun(
            id=run_id,
            conversation_id=data.conversation_id,
            response_id=data.response_id,
            triggering_message_id=data.triggering_message_id,
            project_path=data.project_path,
            task_description=data.task_description,
            harness_id=data.harness_id,
            requested_route_id=data.requested_route_id,
            selected_provider=data.selected_provider,
            selected_model=data.selected_model,
            reasoning_effort=data.reasoning_effort,
            permission_mode=data.permission_mode,
            omniroute_decision_id=data.omniroute_decision_id,
            selection_strategy=data.selection_strategy,
            billing_class=data.billing_class,
            fallback_used=data.fallback_used,
            terminal_status=encode_task_run_status("running"),
            execution_status="running",
            evaluation_status="not_requested",
            execution_started_at=now,
            last_useful_activity_at=now,
            # A routing proposal is not execution evidence.  These fields are
            # filled only by a future execution-path provenance observation.
            actual_provider=None,
            actual_provider_model=None,
            actual_provenance_verified=False,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        with self._session() as session:
            session.add(row)
            session.flush()  # populate defaults + raise CHECK before commit
            return _run_row_to_entity(row)

    def get_run(self, task_run_id: str) -> TaskRun | None:
        """Return a single ``task_runs`` row by id. See base class."""
        with self._session() as session:
            row = session.get(SqlTaskRun, (current_workspace_id(), task_run_id))
            return _run_row_to_entity(row) if row is not None else None

    def get_run_for_response(self, response_id: str, conversation_id: str) -> TaskRun | None:
        stmt = (
            select(SqlTaskRun)
            .where(
                SqlTaskRun.workspace_id == current_workspace_id(),
                SqlTaskRun.response_id == response_id,
                SqlTaskRun.conversation_id == conversation_id,
            )
            .order_by(SqlTaskRun.updated_at.desc())
            .limit(1)
        )
        with self._session() as session:
            row = session.execute(stmt).scalars().first()
            return _run_row_to_entity(row) if row is not None else None

    def get_run_for_conversation(self, task_run_id: str, conversation_id: str) -> TaskRun | None:
        """Return a row only when its conversation matches. See base class."""
        with self._session() as session:
            row = session.get(SqlTaskRun, (current_workspace_id(), task_run_id))
            if row is None or row.conversation_id != conversation_id:
                return None
            return _run_row_to_entity(row)

    def update_run_terminal(self, data: UpdateTaskRunTerminalInput) -> TaskRun | None:
        """Mark a ``task_runs`` row terminal. See base class."""
        now = now_epoch()
        with self._session() as session:
            row = session.get(SqlTaskRun, (current_workspace_id(), data.task_run_id))
            if row is None:
                return None
            # Compare-and-set: late terminal callbacks cannot resurrect a
            # cancelled/timed-out run or overwrite its first finish time.
            if row.execution_status not in {"queued", "starting", "running", "cancelling"}:
                # Preserve historical API behaviour for duplicate callbacks;
                # authoritative execution_finished_at remains immutable.
                row.terminal_at = data.terminal_at
                row.updated_at = now
                session.flush()
                return _run_row_to_entity(row)
            status = (
                "timed_out"
                if data.terminal_status == "incomplete"
                and data.failure_error_code in {"timeout", "deadline_exceeded"}
                else data.terminal_status
            )
            if status not in {"completed", "failed", "cancelled", "timed_out"}:
                raise ValueError(f"invalid execution terminal status: {status}")
            legacy_status = "failed" if status == "timed_out" else status
            row.execution_status = status
            row.terminal_status = encode_task_run_status(legacy_status)
            row.execution_finished_at = data.terminal_at
            row.terminal_at = data.terminal_at
            started = row.execution_started_at or row.started_at
            duration = (data.terminal_at - started) * 1000 if started is not None else None
            row.execution_duration_ms = max(1, duration) if duration is not None else None
            row.duration_ms = row.execution_duration_ms
            if status == "timed_out":
                row.timeout_type = data.failure_error_code or "stream_inactivity"
            if data.response_id is not None:
                row.response_id = data.response_id
            if data.triggering_message_id is not None:
                row.triggering_message_id = data.triggering_message_id
            if data.input_tokens is not None:
                row.input_tokens = data.input_tokens
            if data.output_tokens is not None:
                row.output_tokens = data.output_tokens
            if data.total_cost_usd is not None:
                row.total_cost_usd = data.total_cost_usd
            if data.response_summary is not None:
                row.response_summary = data.response_summary
            if data.changed_files is not None:
                row.changed_files_json = encode_json_list(data.changed_files)
            if data.commit_sha is not None:
                row.commit_sha = data.commit_sha
            if data.failure_error_code is not None:
                row.failure_error_code = data.failure_error_code
            if data.failure_error_message is not None:
                row.failure_error_message = data.failure_error_message
            row.updated_at = now
            session.flush()
            return _run_row_to_entity(row)

    def list_runs_for_conversation(
        self,
        conversation_id: str,
        limit: int = 50,
    ) -> list[TaskRun]:
        """Return runs for *conversation_id* newest-first. See base class."""
        stmt = (
            select(SqlTaskRun)
            .where(
                SqlTaskRun.workspace_id == current_workspace_id(),
                SqlTaskRun.conversation_id == conversation_id,
            )
            .order_by(SqlTaskRun.started_at.desc(), SqlTaskRun.id)
            .limit(limit)
        )
        with self._session() as session:
            return [_run_row_to_entity(r) for r in session.execute(stmt).scalars().all()]

    def list_unreviewed_runs(
        self,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[TaskRun]:
        """Return terminal runs that have no review row. See base class."""
        # ``NOT EXISTS`` against ``task_reviews`` — filters out any
        # run that already has at least one review row from any user.
        # Terminal status is required: a still-running task is not
        # reviewable.
        terminal_codes = [
            encode_task_run_status("completed"),
            encode_task_run_status("failed"),
            encode_task_run_status("cancelled"),
            encode_task_run_status("incomplete"),
        ]
        review_exists = (
            select(SqlTaskReview.task_run_id)
            .where(
                SqlTaskReview.workspace_id == SqlTaskRun.workspace_id,
                SqlTaskReview.task_run_id == SqlTaskRun.id,
            )
            .exists()
        )
        stmt = (
            select(SqlTaskRun)
            .where(
                SqlTaskRun.workspace_id == current_workspace_id(),
                SqlTaskRun.terminal_status.in_(terminal_codes),
                ~review_exists,
            )
            .order_by(SqlTaskRun.started_at.desc(), SqlTaskRun.id)
            .limit(limit)
        )
        if conversation_id is not None:
            stmt = stmt.where(SqlTaskRun.conversation_id == conversation_id)
        with self._session() as session:
            return [_run_row_to_entity(r) for r in session.execute(stmt).scalars().all()]

    def set_langfuse_trace_ids(
        self,
        task_run_id: str,
        trace_id: str,
        observation_id: str,
    ) -> None:
        """Stamp Langfuse trace + observation ids. See base class."""
        with self._session() as session:
            row = session.get(SqlTaskRun, (current_workspace_id(), task_run_id))
            if row is None:
                return
            row.langfuse_trace_id = trace_id
            row.langfuse_observation_id = observation_id
            row.updated_at = now_epoch()

    # ── task_evaluations ──────────────────────────────────────────────

    def create_evaluation(self, data: CreateTaskEvaluationInput) -> TaskEvaluation:
        """INSERT a ``task_evaluations`` row. See base class."""
        now = now_epoch()
        row = SqlTaskEvaluation(
            id=_generate_evaluation_id(),
            task_run_id=data.task_run_id,
            evaluator_type=encode_task_evaluation_type(data.evaluator_type),
            evaluator_provider=data.evaluator_provider,
            evaluator_model=data.evaluator_model,
            evaluator_route_id=data.evaluator_route_id,
            verdict=data.verdict,
            confidence=data.confidence,
            quality_score=data.quality_score,
            proposed_task_family=data.proposed_task_family,
            reasoning=data.reasoning,
            evidence_json=encode_json_list(data.evidence),
            unresolved_issues_json=encode_json_list(data.unresolved_issues),
            created_at=now,
        )
        with self._session() as session:
            run = session.get(SqlTaskRun, (current_workspace_id(), data.task_run_id))
            if run is None:
                raise ValueError(f"unknown task run: {data.task_run_id}")
            session.add(row)
            # Store callers may create deterministic evidence while running;
            # only the recorder schedules LLM evaluation and it does so after
            # an execution terminal transition.  Crucially this update never
            # touches execution_status or the legacy terminal projection.
            if run.execution_status in {"completed", "failed", "cancelled", "timed_out"}:
                run.evaluation_status = (
                    "skipped" if data.verdict == "inconclusive" else "completed"
                )
                run.evaluation_finished_at = now
                run.updated_at = now
            session.flush()
            return _evaluation_row_to_entity(row)

    def get_evaluation_for_run(self, task_run_id: str) -> TaskEvaluation | None:
        """Return the most recent evaluation for *task_run_id*. See base class."""
        stmt = (
            select(SqlTaskEvaluation)
            .where(
                SqlTaskEvaluation.workspace_id == current_workspace_id(),
                SqlTaskEvaluation.task_run_id == task_run_id,
            )
            .order_by(SqlTaskEvaluation.created_at.desc(), SqlTaskEvaluation.id)
            .limit(1)
        )
        with self._session() as session:
            row = session.execute(stmt).scalars().first()
            return _evaluation_row_to_entity(row) if row is not None else None

    # ── task_reviews ──────────────────────────────────────────────────

    def upsert_review(self, data: UpsertTaskReviewInput) -> TaskReview:
        """INSERT-or-UPDATE a ``task_reviews`` row. See base class."""
        now = now_epoch()
        with self._session() as session:
            existing = (
                session.execute(
                    select(SqlTaskReview).where(
                        SqlTaskReview.workspace_id == current_workspace_id(),
                        SqlTaskReview.task_run_id == data.task_run_id,
                        SqlTaskReview.created_by == data.created_by,
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                existing.verdict = data.verdict
                existing.quality_score = data.quality_score
                existing.final_task_family = data.final_task_family
                existing.evaluator_accuracy = data.evaluator_accuracy
                existing.comments = data.comments
                existing.review_action = data.review_action
                existing.learning_eligible = data.learning_eligible
                existing.route_fit = data.route_fit
                existing.failure_attribution = data.failure_attribution
                existing.preferred_route_id = data.preferred_route_id
                existing.preferred_reasoning_effort = data.preferred_reasoning_effort
                existing.source_evaluation_id = data.source_evaluation_id
                existing.review_schema_version = data.review_schema_version
                existing.updated_at = now
                session.flush()
                return _review_row_to_entity(existing)
            row = SqlTaskReview(
                id=_generate_review_id(),
                task_run_id=data.task_run_id,
                verdict=data.verdict,
                quality_score=data.quality_score,
                final_task_family=data.final_task_family,
                evaluator_accuracy=data.evaluator_accuracy,
                comments=data.comments,
                created_by=data.created_by,
                review_action=data.review_action,
                learning_eligible=data.learning_eligible,
                route_fit=data.route_fit,
                failure_attribution=data.failure_attribution,
                preferred_route_id=data.preferred_route_id,
                preferred_reasoning_effort=data.preferred_reasoning_effort,
                source_evaluation_id=data.source_evaluation_id,
                review_schema_version=data.review_schema_version,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return _review_row_to_entity(row)

    def get_review_for_run(
        self,
        task_run_id: str,
        created_by: str | None,
    ) -> TaskReview | None:
        """Return the reviewer's review row. See base class."""
        stmt = select(SqlTaskReview).where(
            SqlTaskReview.workspace_id == current_workspace_id(),
            SqlTaskReview.task_run_id == task_run_id,
            SqlTaskReview.created_by == created_by,
        )
        with self._session() as session:
            row = session.execute(stmt).scalars().first()
            return _review_row_to_entity(row) if row is not None else None

    def list_learning_reviews(self, limit: int = 100) -> list[TaskReview]:
        stmt = (
            select(SqlTaskReview)
            .join(
                SqlTaskRun,
                (SqlTaskRun.workspace_id == SqlTaskReview.workspace_id)
                & (SqlTaskRun.id == SqlTaskReview.task_run_id),
            )
            .where(
                SqlTaskReview.workspace_id == current_workspace_id(),
                SqlTaskReview.review_action.in_(("accepted", "adjusted")),
                SqlTaskReview.learning_eligible.is_(True),
                SqlTaskReview.verdict.in_(("success", "partial", "failure", "unsure")),
                (SqlTaskRun.response_id.is_not(None) | SqlTaskRun.harness_id.is_not(None)),
            )
            .order_by(SqlTaskReview.updated_at.desc())
            .limit(limit)
        )
        with self._session() as session:
            return [_review_row_to_entity(row) for row in session.execute(stmt).scalars().all()]

    def get_any_review_for_run(self, task_run_id: str) -> TaskReview | None:
        """Return any review row for *task_run_id*. See base class."""
        stmt = (
            select(SqlTaskReview)
            .where(
                SqlTaskReview.workspace_id == current_workspace_id(),
                SqlTaskReview.task_run_id == task_run_id,
            )
            .order_by(SqlTaskReview.updated_at.desc(), SqlTaskReview.id)
            .limit(1)
        )
        with self._session() as session:
            row = session.execute(stmt).scalars().first()
            return _review_row_to_entity(row) if row is not None else None

    # ── langfuse_sync_outbox ──────────────────────────────────────────

    def enqueue_langfuse_event(self, data: EnqueueLangfuseEventInput) -> LangfuseOutboxRow:
        """INSERT a ``langfuse_sync_outbox`` row. See base class."""
        now = now_epoch()
        next_attempt = data.next_attempt_at if data.next_attempt_at is not None else now
        payload_bytes = json.dumps(data.payload, ensure_ascii=True).encode("utf-8")
        row = SqlLangfuseSyncOutbox(
            id=_generate_outbox_id(),
            task_run_id=data.task_run_id,
            task_evaluation_id=data.task_evaluation_id,
            event_type=data.event_type,
            idempotency_key=data.idempotency_key,
            payload_json=payload_bytes,
            status=encode_langfuse_outbox_status("pending"),
            attempt_count=0,
            last_error=None,
            next_attempt_at=next_attempt,
            created_at=now,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return _outbox_row_to_entity(row)

    def mark_langfuse_skipped(self, task_run_id: str) -> int:
        """Insert ``status='skipped'`` audit rows. See base class."""
        # When Langfuse is unconfigured, write a single
        # ``status='skipped'`` audit row tagged ``event_type='task_root'``
        # so the review-card UI can still answer "did we try to sync this
        # run to Langfuse?". A separate per-evaluation row is written
        # when the LLM evaluator runs and the no-Langfuse path is taken
        # at that point — same shape, ``status='skipped'``,
        # ``event_type='llm_verdict'``.
        now = now_epoch()
        row = SqlLangfuseSyncOutbox(
            id=_generate_outbox_id(),
            task_run_id=task_run_id,
            task_evaluation_id=None,
            event_type="task_root",
            idempotency_key=f"task:{task_run_id}:root:skipped:v1",
            payload_json=json.dumps({"reason": "LANGFUSE_* env unset; sync disabled"}).encode(
                "utf-8"
            ),
            status=encode_langfuse_outbox_status("skipped"),
            attempt_count=0,
            last_error=None,
            next_attempt_at=now,
            created_at=now,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return 1

    def claim_due_langfuse_events(self, *, now: int, limit: int = 50) -> list[LangfuseOutboxRow]:
        """Return up to *limit* ``pending`` rows whose retry time is past."""
        pending_code = encode_langfuse_outbox_status("pending")
        stmt = (
            select(SqlLangfuseSyncOutbox)
            .where(
                SqlLangfuseSyncOutbox.workspace_id == current_workspace_id(),
                SqlLangfuseSyncOutbox.status == pending_code,
                SqlLangfuseSyncOutbox.next_attempt_at <= now,
            )
            .order_by(SqlLangfuseSyncOutbox.next_attempt_at, SqlLangfuseSyncOutbox.id)
            .limit(limit)
        )
        with self._session() as session:
            return [_outbox_row_to_entity(r) for r in session.execute(stmt).scalars().all()]

    def mark_langfuse_delivered(self, outbox_id: str, delivered_at: int) -> None:
        """Advance ``status='delivered'``. See base class."""
        delivered_code = encode_langfuse_outbox_status("delivered")
        with self._session() as session:
            row = session.get(SqlLangfuseSyncOutbox, (current_workspace_id(), outbox_id))
            if row is None:
                return
            row.status = delivered_code
            row.delivered_at = delivered_at
            row.last_error = None

    def mark_langfuse_failed(
        self,
        outbox_id: str,
        last_error: str,
        next_attempt_at: int,
    ) -> None:
        """Bump ``attempt_count`` + record error + advance ``next_attempt_at``."""
        with self._session() as session:
            row = session.get(SqlLangfuseSyncOutbox, (current_workspace_id(), outbox_id))
            if row is None:
                return
            row.attempt_count = (row.attempt_count or 0) + 1
            row.last_error = last_error
            row.next_attempt_at = next_attempt_at
            # Status stays ``pending`` so the worker re-tries.

    def mark_langfuse_dead(self, outbox_id: str, last_error: str) -> None:
        """Mark ``status='dead'`` after the retry budget is exhausted."""
        dead_code = encode_langfuse_outbox_status("dead")
        with self._session() as session:
            row = session.get(SqlLangfuseSyncOutbox, (current_workspace_id(), outbox_id))
            if row is None or row.status == dead_code:
                return
            row.status = dead_code
            row.last_error = last_error

    def count_pending_langfuse_events(self, task_run_id: str) -> int:
        """Count ``status='pending'`` rows for *task_run_id*."""
        pending_code = encode_langfuse_outbox_status("pending")
        stmt = select(func.count(SqlLangfuseSyncOutbox.id)).where(
            SqlLangfuseSyncOutbox.workspace_id == current_workspace_id(),
            SqlLangfuseSyncOutbox.task_run_id == task_run_id,
            SqlLangfuseSyncOutbox.status == pending_code,
        )
        with self._session() as session:
            return int(session.execute(stmt).scalar() or 0)

    # ── aggregate ─────────────────────────────────────────────────────

    def get_run_detail(self, task_run_id: str) -> TaskRunDetail | None:
        """Return a :class:`TaskRunDetail` aggregate. See base class."""
        run = self.get_run(task_run_id)
        if run is None:
            return None
        evaluation = self.get_evaluation_for_run(task_run_id)
        # ``get_any_review_for_run`` returns the most-recent reviewer;
        # for the review-card UI we want the requesting user's review
        # when available, falling back to any review. The route layer
        # filters by ``created_by`` AFTER this aggregate.
        review = self.get_any_review_for_run(task_run_id)
        langfuse_pending = self.count_pending_langfuse_events(task_run_id) > 0
        return TaskRunDetail(
            run=run,
            evaluation=evaluation,
            review=review,
            langfuse_pending=langfuse_pending,
        )


__all__ = ["SqlAlchemyTaskOutcomeStore"]
