"""Task-outcome store: task_runs / task_evaluations / task_reviews / langfuse_sync_outbox.

Abstract base for the four tables backing the task-outcome tracking
vertical slice. The SQLAlchemy implementation lives in
:mod:`omnigent.stores.task_outcome_store.sqlalchemy_store` and
mirrors the structure of :mod:`omnigent.stores.comment_store`:
a single :class:`TaskOutcomeStore` exposing the cross-table
operations needed by the relay / evaluator / routes, and the four
rows are conceptually one aggregate because every query in the
review-card UI joins ``task_runs`` → ``task_evaluations`` +
``task_reviews`` → ``langfuse_sync_outbox``.

Store-level invariants worth keeping in mind:

- **No DELETE.** Every row is preserved for audit. Review corrections
  UPDATE in place (via the unique constraint on
  ``task_reviews(task_run_id, created_by)``); Langfuse rows stay
  ``pending`` / ``delivered`` / ``dead`` / ``skipped`` forever.
- **Routing snapshot is immutable after first write.** A re-PATCH on
  the conversation (the user changing route mid-session) does not
  rewrite the ``task_runs`` row.
- **LLM evaluation is append-only.** A human review disagreement
  never overwrites the LLM row.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod

from omnigent.entities import (
    LangfuseOutboxRow,
    TaskEvaluation,
    TaskReview,
    TaskRun,
    TaskRunModelCall,
)


@dataclasses.dataclass(frozen=True)
class TaskRunDetail:
    """Aggregate returned by :meth:`TaskOutcomeStore.get_run`.

    Binds a run to its (at most one) LLM evaluation, the user's
    review, and the Langfuse sync status — exactly what the
    review-card UI renders.

    :param run: The :class:`TaskRun` row.
    :param evaluation: The :class:`TaskEvaluation` (always present
        once a run reaches a terminal state; ``None`` if the run
        hasn't yet reached terminal status).
    :param review: The :class:`TaskReview` for the requesting
        user (``None`` when not yet reviewed by them).
    :param langfuse_pending: ``True`` when at least one
        ``langfuse_sync_outbox`` row for this run is still
        ``status='pending'``. UI uses this to flag "syncing to
        Langfuse…" / "Langfuse sync failed" badges.
    """

    run: TaskRun
    evaluation: TaskEvaluation | None
    review: TaskReview | None
    langfuse_pending: bool


@dataclasses.dataclass(frozen=True)
class CreateTaskRunInput:
    """All fields needed to create a fresh ``task_runs`` row.

    Bundles the immutable routing snapshot the relay captured at
    ``response.in_progress``. See
    :class:`omnigent.entities.task_outcome.TaskRun` for field docs.

    ``terminal_status`` is required even though it is technically
    mutable — the relay always writes ``"running"`` on the
    initial INSERT so the ``CHECK`` admits every state without a
    follow-up UPDATE. The store still accepts a terminal status
    on insert for tests / parity with the SQLAlchemy layer.
    """

    conversation_id: str
    terminal_status: str = "running"
    response_id: str | None = None
    triggering_message_id: str | None = None
    project_path: str | None = None
    task_description: str | None = None
    harness_id: str | None = None
    requested_route_id: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None
    omniroute_decision_id: str | None = None
    selection_strategy: str | None = None
    billing_class: str | None = None
    fallback_used: bool | None = None


@dataclasses.dataclass(frozen=True)
class UpdateTaskRunTerminalInput:
    """All fields needed to mark a ``task_runs`` row terminal.

    The relay sends only the fields it observed in the terminal
    event; everything else stays as it was on the row (so a
    ``response.completed`` that arrives WITHOUT a usage bucket
    doesn't clear an earlier input_tokens total — there's none, but
    the pattern generalises for future event shapes).

    :param task_run_id: UUID of the existing row.
    :param terminal_status: ``completed`` / ``failed`` / ``cancelled``
        / ``incomplete``.
    :param terminal_at: Unix epoch seconds the terminal event was
        observed.
    :param response_id: Set when ``response.in_progress`` arrived
        AFTER the relay's first INSERT (e.g. the routing approval
        path emits the run row before the harness has emitted
        ``response.in_progress``).
    :param input_tokens: Total input tokens across the turn.
    :param output_tokens: Total output tokens across the turn.
    :param total_cost_usd: Catalog-priced or harness-reported cost.
    :param response_summary: Truncated final assistant response.
    :param changed_files: List of changed file paths (already
        sanitised by the relay).
    :param commit_sha: Git commit SHA when surfaced.
    :param failure_error_code: Error code from the terminal event.
    :param failure_error_message: Truncated error message.
    """

    task_run_id: str
    terminal_status: str
    terminal_at: int
    response_id: str | None = None
    triggering_message_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    response_summary: str | None = None
    changed_files: list[str] | None = None
    commit_sha: str | None = None
    failure_error_code: str | None = None
    failure_error_message: str | None = None


@dataclasses.dataclass(frozen=True)
class CreateTaskRunModelCallInput:
    """Sanitized metadata extracted from a single outbound model request."""

    task_run_id: str
    conversation_id: str
    ordinal: int
    correlation_id: str
    requested_provider: str
    requested_model: str
    started_at: int
    requested_reasoning: str | None = None
    effective_reasoning: str | None = None
    stream: bool | None = None
    opencode_session_id: str | None = None


@dataclasses.dataclass(frozen=True)
class CompleteTaskRunModelCallInput:
    task_run_id: str
    correlation_id: str
    request_status: str
    finished_at: int
    http_status: int | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    omniroute_request_id: str | None = None
    omniroute_decision_id: str | None = None
    fallback_used: bool | None = None
    selection_strategy: str | None = None
    billing_class: str | None = None
    provenance_verified: bool = False
    failure_stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclasses.dataclass(frozen=True)
class CreateTaskEvaluationInput:
    """All fields needed to create a ``task_evaluations`` row.

    See :class:`omnigent.entities.task_outcome.TaskEvaluation` for
    field docs. Append-only: callers don't UPDATE evaluations.
    """

    task_run_id: str
    evaluator_type: str
    verdict: str
    evaluator_provider: str | None = None
    evaluator_model: str | None = None
    evaluator_route_id: str | None = None
    confidence: float | None = None
    quality_score: int | None = None
    proposed_task_family: str | None = None
    reasoning: str | None = None
    evidence: list[str] | None = None
    unresolved_issues: list[str] | None = None


@dataclasses.dataclass(frozen=True)
class UpsertTaskReviewInput:
    """All fields for upserting a ``task_reviews`` row by
    ``(task_run_id, created_by)``.

    The store SELECTs an existing row for the
    ``(task_run_id, created_by)`` pair, then either UPDATEs it in
    place (when one exists) or INSERTs a fresh one. Keeps the
    API "submit / correct review" path symmetric: same body, same
    semantics.

    :param task_run_id: Owning :class:`TaskRun.id`.
    :param verdict: One of ``REVIEW_VERDICTS``.
    :param created_by: Reviewer email / id (``None`` for
        single-user mode).
    :param quality_score: 1–5 from the human, optional.
    :param final_task_family: Task family the human picked.
    :param evaluator_accuracy: ``correct`` / ``partly_correct`` /
        ``incorrect`` / ``unsure``.
    :param comments: Optional free-text.
    """

    task_run_id: str
    verdict: str
    created_by: str | None = None
    quality_score: int | None = None
    final_task_family: str | None = None
    evaluator_accuracy: str | None = None
    comments: str | None = None
    review_action: str = "accepted"
    learning_eligible: bool = True
    route_fit: str | None = None
    failure_attribution: str | None = None
    preferred_route_id: str | None = None
    preferred_reasoning_effort: str | None = None
    source_evaluation_id: str | None = None
    review_schema_version: int = 1


@dataclasses.dataclass(frozen=True)
class EnqueueLangfuseEventInput:
    """All fields needed to append a ``langfuse_sync_outbox`` row.

    See :class:`omnigent.entities.task_outcome.LangfuseOutboxRow`
    for field docs. Rows are never deleted — once the worker
    delivers the payload to Langfuse it sets ``status='delivered'``
    + ``delivered_at`` and stops touching the row.
    """

    task_run_id: str
    event_type: str
    idempotency_key: str
    payload: dict[str, object]
    task_evaluation_id: str | None = None
    next_attempt_at: int | None = None  # default: now


class TaskOutcomeStore(ABC):
    """Abstract base for task-outcome persistence.

    Covers the four tables backing the task-outcome vertical
    slice: ``task_runs``, ``task_evaluations``,
    ``task_reviews``, ``langfuse_sync_outbox``. The operations are
    grouped by table for clarity, with the cross-table
    :meth:`get_run` aggregate (used by the review-card UI) at the
    bottom.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the task-outcome store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    # ── task_runs ─────────────────────────────────────────────────────

    @abstractmethod
    def create_run(self, data: CreateTaskRunInput) -> TaskRun:
        """INSERT a fresh ``task_runs`` row.

        Returns the new :class:`TaskRun` with the store-assigned
        ``id`` / ``created_at`` / ``updated_at``.
        """

    @abstractmethod
    def get_run(self, task_run_id: str) -> TaskRun | None:
        """Return a single :class:`TaskRun` row by id, or ``None``."""

    @abstractmethod
    def get_run_for_response(self, response_id: str, conversation_id: str) -> TaskRun | None:
        """Return the task run for an exact terminal response in a session."""

    @abstractmethod
    def get_run_for_conversation(self, task_run_id: str, conversation_id: str) -> TaskRun | None:
        """Return a :class:`TaskRun` only when it belongs to *conversation_id*.

        Used by the route layer to enforce session ownership
        without exposing run rows from other sessions. ``None``
        when the run doesn't exist OR exists but belongs to a
        different conversation.
        """

    @abstractmethod
    def update_run_terminal(self, data: UpdateTaskRunTerminalInput) -> TaskRun | None:
        """Mark a ``task_runs`` row terminal + write usage / failure fields.

        Computes ``duration_ms = terminal_at - started_at`` on
        success. Returns the updated row, or ``None`` when the id
        doesn't exist.
        """

    @abstractmethod
    def create_model_call(self, data: CreateTaskRunModelCallInput) -> TaskRunModelCall:
        """Create an individual model request; duplicate correlations are idempotent."""

    @abstractmethod
    def complete_model_call(self, data: CompleteTaskRunModelCallInput) -> TaskRunModelCall | None:
        """Complete a call without allowing a completed call to regress."""

    @abstractmethod
    def list_model_calls(self, task_run_id: str) -> list[TaskRunModelCall]:
        """Return model calls in deterministic request ordinal order."""

    @abstractmethod
    def list_runs_for_conversation(
        self,
        conversation_id: str,
        limit: int = 50,
    ) -> list[TaskRun]:
        """Return runs for *conversation_id* newest-first, capped at *limit*."""

    @abstractmethod
    def list_unreviewed_runs(
        self,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[TaskRun]:
        """Return terminal runs that have no review row from any user.

        Used by the ``GET /v1/sessions/{id}/unreviewed-task-outcomes``
        endpoint. Filters in SQL via a ``NOT EXISTS`` subquery so
        a session with many terminal runs doesn't load every
        review row just to discard them.

        :param conversation_id: Optional session filter (``None``
            returns unreviewed across every session the caller
            can see — currently unused; left in the API surface
            for a future "all unreviewed" admin view).
        :param limit: Hard cap on rows returned.
        """

    @abstractmethod
    def set_langfuse_trace_ids(
        self,
        task_run_id: str,
        trace_id: str,
        observation_id: str,
    ) -> None:
        """Stamp Langfuse trace + observation ids onto a :class:`TaskRun`.

        Called by the Langfuse adapter after the first successful
        POST of the root-observation payload, so a subsequent
        ``GET /v1/task-runs/{id}`` can link straight to the
        Langfuse UI.
        """

    # ── task_evaluations ──────────────────────────────────────────────

    @abstractmethod
    def create_evaluation(self, data: CreateTaskEvaluationInput) -> TaskEvaluation:
        """INSERT a ``task_evaluations`` row.

        Append-only: there is intentionally no
        ``update_evaluation`` operation. An evaluator failure is
        captured by inserting a ``verdict='inconclusive'`` row, not
        by mutating an earlier one.
        """

    @abstractmethod
    def get_evaluation_for_run(self, task_run_id: str) -> TaskEvaluation | None:
        """Return the (single) evaluation for a run, or ``None``.

        The schema permits multiple evaluator rows per run (e.g.
        for future deterministic + LLM pairs), but the review-card
        UI renders exactly one. We return the most-recently-created
        row.
        """

    # ── task_reviews ──────────────────────────────────────────────────

    @abstractmethod
    def upsert_review(self, data: UpsertTaskReviewInput) -> TaskReview:
        """INSERT-or-UPDATE a ``task_reviews`` row.

        Scoped by ``(task_run_id, created_by)``. The first submit
        creates the row; subsequent submits UPDATE it in place so
        a reviewer can correct their judgment without polluting
        the table with history rows. The unique index on
        ``(workspace_id, task_run_id, created_by)`` makes the
        ``SELECT … existing`` branch atomic — no two concurrent
        submits by the same reviewer can both create a row.
        """

    @abstractmethod
    def get_review_for_run(
        self,
        task_run_id: str,
        created_by: str | None,
    ) -> TaskReview | None:
        """Return the reviewer's review row for *task_run_id*, or ``None``."""

    @abstractmethod
    def list_learning_reviews(self, limit: int = 100) -> list[TaskReview]:
        """Return only approved reviews with usable task provenance."""

    @abstractmethod
    def get_any_review_for_run(self, task_run_id: str) -> TaskReview | None:
        """Return any review row for *task_run_id*, newest first.

        Used by the unreviewed-runs query — a run with at least
        one review is considered reviewed.
        """

    # ── langfuse_sync_outbox ──────────────────────────────────────────

    @abstractmethod
    def enqueue_langfuse_event(self, data: EnqueueLangfuseEventInput) -> LangfuseOutboxRow:
        """INSERT a ``langfuse_sync_outbox`` row.

        Always written with ``status='pending'`` (default). The
        worker advances ``status`` as it processes the row. When
        Langfuse is unconfigured the caller writes
        ``status='skipped'`` directly via :meth:`mark_langfuse_skipped`.
        """

    @abstractmethod
    def mark_langfuse_skipped(self, task_run_id: str) -> int:
        """Insert ``status='skipped'`` audit rows for a run, returning the count.

        Called by the relay when a run reaches a terminal state
        and Langfuse is unconfigured (``LANGFUSE_*`` env unset).
        Writes one row per event the live path would otherwise
        have enqueued (root + verdict scores) so audit queries
        can answer "what was the sync status for run X?" with a
        single ``SELECT``.

        :returns: Number of audit rows written.
        """

    @abstractmethod
    def claim_due_langfuse_events(self, *, now: int, limit: int = 50) -> list[LangfuseOutboxRow]:
        """Return up to *limit* ``status='pending'`` rows whose
        ``next_attempt_at <= now``.

        Called by the retry worker every tick. Returns a snapshot
        of rows; the worker is responsible for advancing
        ``status`` on each via :meth:`mark_langfuse_delivered` /
        :meth:`mark_langfuse_failed` / :meth:`mark_langfuse_dead`.
        """

    @abstractmethod
    def mark_langfuse_delivered(self, outbox_id: str, delivered_at: int) -> None:
        """Advance ``status='delivered'`` + ``delivered_at``."""

    @abstractmethod
    def mark_langfuse_failed(
        self,
        outbox_id: str,
        last_error: str,
        next_attempt_at: int,
    ) -> None:
        """Bump ``attempt_count`` + record error + advance ``next_attempt_at``.

        Status stays ``'pending'`` so the next tick re-tries.
        """

    @abstractmethod
    def mark_langfuse_dead(self, outbox_id: str, last_error: str) -> None:
        """Mark the row ``status='dead'`` after the retry budget is exhausted.

        Idempotent: a second call is a no-op.
        """

    @abstractmethod
    def count_pending_langfuse_events(self, task_run_id: str) -> int:
        """Return the number of ``status='pending'`` rows for *task_run_id*.

        Used by :meth:`get_run` to compute ``langfuse_pending`` for
        the review-card UI's "syncing…" / "failed" badge.
        """

    # ── aggregate ─────────────────────────────────────────────────────

    @abstractmethod
    def get_run_detail(self, task_run_id: str) -> TaskRunDetail | None:
        """Return a :class:`TaskRunDetail` aggregate (run + eval + review + langfuse).

        ``None`` when the run doesn't exist.
        """
