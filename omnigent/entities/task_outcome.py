"""Task-outcome entities: TaskRun, TaskEvaluation, TaskReview, LangfuseOutboxRow.

These are the wire-format / domain shapes for the task-outcome tracking
vertical slice. The store layer's SQLAlchemy implementation translates to and
from the matching ``omnigent.db.db_models`` ORM rows; the HTTP / web UI layer
serialises these directly. The shape mirrors the comment-store pattern
(``omnigent.entities.comment``): frozen dataclasses with the same field set as
the underlying table so the store / entity boundary is mechanical.

Captured routing provenance is stable on the row for the lifetime of the
task run — a later ``PATCH /v1/sessions/{id}`` (e.g. the user changing route)
does NOT retroactively rewrite provenance. The LLM evaluation is recorded as
an append-only row; a human review disagreement never overwrites it.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

# ── Task family vocabulary ────────────────────────────────────────────────
#
# Initial task-family options surfaced by the LLM evaluator. Stable names
# so existing analyses keep working across schema versions; new families
# append to the end rather than reusing a shipped name. The human reviewer
# can correct the proposed family via the ``final_task_family`` field on
# ``TaskReview``.

TASK_FAMILIES: tuple[str, ...] = (
    "repository_inspection",
    "planning",
    "small_bug_fix",
    "feature_implementation",
    "test_failure_repair",
    "refactor",
    "frontend",
    "backend_api",
    "database_migration",
    "infrastructure_config",
    "code_review",
    "documentation",
    "other",
)


# Verdict vocabularies are duplicated in the migration CHECK constraints;
# keep both lists in sync. Used by the API layer to validate request bodies
# before they reach the store.
TASK_VERDICTS: tuple[str, ...] = (
    "success",
    "partial",
    "failure",
    "inconclusive",
)
REVIEW_VERDICTS: tuple[str, ...] = (
    "success",
    "partial",
    "failure",
    "unsure",
    "skipped",
)
ROUTE_FIT_VALUES: tuple[str, ...] = (
    "appropriate",
    "too_weak",
    "overkill",
    "wrong_capability",
    "unsure",
)
FAILURE_ATTRIBUTION_VALUES: tuple[str, ...] = (
    "router",
    "model",
    "harness",
    "environment",
    "permissions",
    "task_definition",
    "external_service",
    "unknown",
)
REVIEW_ACTIONS: tuple[str, ...] = ("accepted", "adjusted", "declined")
REASONING_EFFORT_VALUES: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

EVALUATOR_ACCURACY_VALUES: tuple[str, ...] = (
    "correct",
    "partly_correct",
    "incorrect",
    "unsure",
)
# ``terminal_status`` is retained for API compatibility and is always derived
# from ``execution_status``. Evaluation activity must never change it.
TASK_RUN_STATUSES: tuple[str, ...] = (
    "queued",
    "starting",
    "running",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
    "timed_out",
    "incomplete",  # legacy wire value
)
EVALUATION_STATUSES: tuple[str, ...] = (
    "not_requested",
    "pending",
    "completed",
    "deferred",
    "skipped",
    "failed",
)
OUTBOX_STATUSES: tuple[str, ...] = (
    "pending",
    "delivered",
    "dead",
    "skipped",
)


@dataclasses.dataclass(frozen=True)
class RoutingProposal:
    """Immutable routing recommendation and bounded input audit record."""

    id: str
    conversation_id: str
    elicitation_id: str
    user_message_sha256: str
    user_message_excerpt: str
    user_message_chars: int
    content_types: list[str]
    original_route_id: str
    requires_explicit_approval: bool
    proposal_payload_excerpt: str
    proposal_payload_sha256: str
    created_at: int
    original_harness: str | None = None
    original_provider: str | None = None
    original_model: str | None = None
    original_reasoning_effort: str | None = None
    original_permission_mode: str | None = None
    evaluator_route_id: str | None = None
    evaluator_provider: str | None = None
    evaluator_model: str | None = None
    evaluator_billing_class: str | None = None
    evaluator_fallback_used: bool | None = None
    evaluator_decision_id: str | None = None
    evaluator_selection_strategy: str | None = None


@dataclasses.dataclass(frozen=True)
class RoutingDecision:
    """One immutable approve/change/decline decision for a proposal."""

    id: str
    proposal_id: str
    action: str
    decision_request_sha256: str
    original_route_id: str
    decision_payload_excerpt: str
    decision_payload_sha256: str
    created_at: int
    original_harness: str | None = None
    original_provider: str | None = None
    original_model: str | None = None
    original_reasoning_effort: str | None = None
    original_permission_mode: str | None = None
    final_harness: str | None = None
    final_provider: str | None = None
    final_model: str | None = None
    final_route_id: str | None = None
    final_reasoning_effort: str | None = None
    final_permission_mode: str | None = None
    decided_by: str | None = None


@dataclasses.dataclass
class TaskRun:
    """One routed coding execution attempt initiated by a user message.

    Created on the first ``response.in_progress`` for a turn (when the
    harness actually starts executing); the routing snapshot on the row is
    immutable thereafter so a later ``PATCH /v1/sessions/{id}`` doesn't
    retroactively rewrite provenance.

    :param id: UUID, e.g. ``"tr_abc123"``.
    :param conversation_id: Owning session id, e.g. ``"conv_abc123"``.
    :param response_id: Harness-side task id (``None`` until
        ``response.in_progress`` arrives).
    :param triggering_message_id: User message item id (when known).
    :param project_path: Free-form project / repo identifier at task
        start. Sanitized by the writer.
    :param task_description: Sanitized, bounded summary of the user
        request.
    :param proposed_task_family: Family proposed by the LLM evaluator;
        ``None`` until evaluation runs.
    :param estimated_difficulty: Optional difficulty hint from the
        routing agent.
    :param harness_id: Harness name resolved at task start.
    :param requested_route_id: Native OmniRoute route id the routing
        agent proposed.
    :param selected_provider: Concrete provider resolved by OmniRoute.
    :param selected_model: Concrete model resolved by OmniRoute.
    :param reasoning_effort: Routing snapshot.
    :param permission_mode: Routing snapshot.
    :param omniroute_decision_id: Stable per-call decision id.
    :param selection_strategy: Strategy used by OmniRoute.
    :param billing_class: Billing class for the resolved model.
    :param fallback_used: ``True`` when OmniRoute fell back to a
        secondary model.
    :param terminal_status: One of ``TASK_RUN_STATUSES``.
    :param started_at: Unix epoch seconds the run row was created.
    :param terminal_at: Unix epoch seconds the terminal event was
        observed.
    :param duration_ms: ``terminal_at - started_at`` in milliseconds.
    :param input_tokens: Total input tokens across the turn.
    :param output_tokens: Total output tokens across the turn.
    :param total_cost_usd: Catalog-priced or harness-reported USD
        cost.
    :param response_summary: Truncated final assistant response.
    :param changed_files: List of changed file paths (decoded from
        ``changed_files_json``). ``None`` when not surfaced.
    :param commit_sha: Git commit SHA (when surfaced).
    :param failure_error_code: Error code from
        ``response.failed.error.code`` or
        ``response.incomplete.incomplete_details.reason``.
    :param failure_error_message: Truncated error message.
    :param langfuse_trace_id: Langfuse trace id stamped on first
        successful sync.
    :param langfuse_observation_id: Langfuse root observation id.
    :param created_at: Unix epoch seconds the row was first written.
    :param updated_at: Unix epoch seconds of the last write.
    """

    id: str
    conversation_id: str
    terminal_status: str
    created_at: int
    updated_at: int
    # Independent execution/evaluation state machines.  ``terminal_status``
    # mirrors execution_status for legacy clients only.
    execution_status: str = "running"
    evaluation_status: str = "not_requested"
    execution_started_at: int | None = None
    execution_finished_at: int | None = None
    execution_duration_ms: int | None = None
    evaluation_started_at: int | None = None
    evaluation_finished_at: int | None = None
    evaluation_attempt_count: int = 0
    evaluation_last_attempt_at: int | None = None
    evaluation_next_retry_at: int | None = None
    evaluation_error_kind: str | None = None
    evaluation_error_code: str | None = None
    evaluation_error_message: str | None = None
    evaluation_requested_model: str | None = None
    timeout_type: str | None = None
    last_useful_activity_at: int | None = None
    actual_provider: str | None = None
    actual_provider_model: str | None = None
    actual_provenance_verified: bool | None = None
    response_id: str | None = None
    triggering_message_id: str | None = None
    project_path: str | None = None
    task_description: str | None = None
    proposed_task_family: str | None = None
    estimated_difficulty: str | None = None
    harness_id: str | None = None
    requested_route_id: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None
    omniroute_decision_id: str | None = None
    routing_proposal_id: str | None = None
    routing_decision_id: str | None = None
    selection_strategy: str | None = None
    billing_class: str | None = None
    fallback_used: bool | None = None
    started_at: int | None = None
    terminal_at: int | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    response_summary: str | None = None
    changed_files: list[str] | None = None
    commit_sha: str | None = None
    failure_error_code: str | None = None
    failure_error_message: str | None = None
    langfuse_trace_id: str | None = None
    langfuse_observation_id: str | None = None


@dataclasses.dataclass
class TaskEvaluation:
    """One append-only automated evaluation of a :class:`TaskRun`.

    A row exists only when the automated evaluator successfully returned a
    valid structured judgment. Transport, configuration, provenance, and
    protocol failures are recorded on :class:`TaskRun` instead.

    :param id: UUID, e.g. ``"tev_abc123"``.
    :param task_run_id: Owning :class:`TaskRun.id`.
    :param evaluator_type: ``"deterministic"`` or ``"llm"``.
    :param evaluator_provider: Concrete provider resolved by
        OmniRoute for the evaluation call.
    :param evaluator_model: Concrete model resolved by OmniRoute for
        the evaluation call.
    :param evaluator_route_id: Native OmniRoute route id the
        evaluator used, when known.
    :param verdict: One of ``TASK_VERDICTS``. ``inconclusive`` is
        the "evaluator call failed" path.
    :param confidence: 0.0–1.0 confidence from the LLM.
    :param quality_score: 1–5 from the LLM.
    :param proposed_task_family: Family the evaluator proposes.
    :param reasoning: Free-text reasoning from the evaluator (or a
        bounded error message when the evaluator call failed).
    :param evidence: List of strings the evaluator cited as
        evidence.
    :param unresolved_issues: List of strings the evaluator flagged
        but did not resolve.
    :param created_at: Unix epoch seconds when the row was written.
    """

    id: str
    task_run_id: str
    evaluator_type: str
    verdict: str
    created_at: int
    evaluator_provider: str | None = None
    evaluator_model: str | None = None
    evaluator_route_id: str | None = None
    evaluator_fallback_used: bool | None = None
    evaluator_decision_id: str | None = None
    confidence: float | None = None
    quality_score: int | None = None
    proposed_task_family: str | None = None
    reasoning: str | None = None
    evidence: list[str] | None = None
    unresolved_issues: list[str] | None = None


@dataclasses.dataclass
class TaskReview:
    """One human review of a :class:`TaskRun`.

    Idempotent on ``(task_run_id, created_by)`` — a re-submit UPDATEs
    the existing row instead of appending a duplicate. Stored
    SEPARATELY from the LLM evaluation so a human disagreement
    never overwrites the LLM verdict.

    :param id: UUID, e.g. ``"trv_abc123"``.
    :param task_run_id: Owning :class:`TaskRun.id`.
    :param verdict: One of ``REVIEW_VERDICTS`` (``skipped`` is a
        real state, not absence).
    :param quality_score: 1–5 from the human, optional.
    :param final_task_family: Task family the human picked.
    :param evaluator_accuracy: ``correct`` / ``partly_correct`` /
        ``incorrect`` / ``unsure`` — the human's view of the LLM
        verdict.
    :param comments: Optional free-text.
    :param created_by: Reviewer email / id. ``None`` in single-user
        mode or for legacy rows.
    :param created_at: Unix epoch seconds when the row was first
        written.
    :param updated_at: Unix epoch seconds of the last write.
    """

    id: str
    task_run_id: str
    verdict: str
    created_at: int
    updated_at: int
    quality_score: int | None = None
    final_task_family: str | None = None
    evaluator_accuracy: str | None = None
    comments: str | None = None
    created_by: str | None = None
    review_action: str | None = None
    learning_eligible: bool = False
    route_fit: str | None = None
    failure_attribution: str | None = None
    preferred_route_id: str | None = None
    preferred_reasoning_effort: str | None = None
    source_evaluation_id: str | None = None
    review_schema_version: int = 1


@dataclasses.dataclass
class LangfuseOutboxRow:
    """One row of the Langfuse transactional outbox.

    Rows are never deleted. ``status`` moves ``pending`` → ``delivered``
    or ``pending`` → ``dead`` (retry budget exhausted). ``status='skipped'``
    is the audit-record path when Langfuse is unconfigured.

    :param id: UUID, e.g. ``"lfs_abc123"``.
    :param task_run_id: Owning :class:`TaskRun.id`.
    :param task_evaluation_id: Owning :class:`TaskEvaluation.id`
        when the event is an evaluator row (``None`` for
        trace / human-review-only events).
    :param event_type: ``task_root`` / ``llm_verdict`` /
        ``human_verdict`` / ``human_quality`` /
        ``llm_evaluation_accuracy``.
    :param idempotency_key: Stable Langfuse score id
        (``task:<run>:…:v1``).
    :param payload: Decoded JSON request body the worker will POST.
    :param status: One of ``OUTBOX_STATUSES``.
    :param attempt_count: Number of POST attempts so far.
    :param last_error: Truncated last-error string.
    :param next_attempt_at: Unix epoch seconds the worker should
        next try this row.
    :param created_at: Unix epoch seconds the row was written.
    :param delivered_at: Unix epoch seconds of the successful POST.
    """

    id: str
    task_run_id: str
    event_type: str
    idempotency_key: str
    payload: dict[str, Any]
    status: str
    attempt_count: int
    next_attempt_at: int
    created_at: int
    task_evaluation_id: str | None = None
    last_error: str | None = None
    delivered_at: int | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def decode_json_list(value: str | None) -> list[str] | None:
    """Decode a JSON-encoded list-of-strings column.

    Returns ``None`` when the column is ``None`` or empty. Returns
    ``[]`` when the column is the literal string ``"[]"`` (a normal
    empty list, not "no value"). Used for ``changed_files_json``,
    ``evidence_json``, and ``unresolved_issues_json``.

    :param value: Raw column value (already read from the DB).
    :returns: Decoded list, or ``None`` when not set.
    :raises ValueError: When the column is non-empty but not a JSON
        list of strings. Stores should never produce this; the
        exception is a defensive guard against bad data.
    """
    if value is None or value == "":
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON list column value: {exc}") from exc
    if not isinstance(decoded, list):
        raise ValueError(f"expected a JSON list, got {type(decoded).__name__!r}")
    out: list[str] = []
    for entry in decoded:
        if isinstance(entry, str):
            out.append(entry)
        else:
            # Tolerate non-string entries by coercing; preserves the
            # common shape rather than dropping the row.
            out.append(str(entry))
    return out


def encode_json_list(values: list[str] | None) -> str | None:
    """Encode a list-of-strings for ``*_json`` column storage.

    Returns ``None`` for ``None`` / empty inputs so the column stays
    ``NULL`` rather than carrying the literal string ``"[]"`` —
    keeps "no value" and "empty list" distinct at the DB layer
    (which matters for the API's nullable-shape contract).

    :param values: List of strings, or ``None``.
    :returns: JSON-encoded string, or ``None`` for empty input.
    """
    if values is None or len(values) == 0:
        return None
    return json.dumps(list(values), ensure_ascii=True)
