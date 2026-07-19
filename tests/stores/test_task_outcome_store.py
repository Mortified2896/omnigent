"""Tests for the SQLAlchemy task-outcome store.

Exercises the four-table ``SqlAlchemyTaskOutcomeStore`` directly
against an in-memory SQLite database. Each test seeds a
conversation row first (the FK target) then runs the operation
under test.

Mirrors the structure of
:mod:`tests.stores.test_comment_store` so future store additions
follow the same pattern.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.stores.task_outcome_store import (
    CreateRoutingDecisionInput,
    CreateRoutingProposalInput,
    CreateTaskEvaluationInput,
    CreateTaskRunInput,
    EnqueueLangfuseEventInput,
    RoutingDecisionConflictError,
    TaskOutcomeStore,
    UpdateTaskRunTerminalInput,
    UpsertTaskReviewInput,
)
from omnigent.stores.task_outcome_store.sqlalchemy_store import (
    SqlAlchemyTaskOutcomeStore,
)


@pytest.fixture
def store(tmp_path_factory) -> TaskOutcomeStore:
    """Fresh SQLite-backed store + seeded conversation row."""
    db_path = tmp_path_factory.mktemp("store") / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, created_at, updated_at, "
                "kind, root_conversation_id) VALUES "
                "('c1', 1, 1, 1, 'c1'), ('c2', 2, 2, 1, 'c2')"
            )
        )
    return SqlAlchemyTaskOutcomeStore(uri)


def _create_run(store: TaskOutcomeStore, *, conv: str = "c1", **kwargs) -> str:
    """Helper: create a task_run with sensible defaults + return its id."""
    defaults = {
        "conversation_id": conv,
        "response_id": "r1",
        "task_description": "Fix the login bug",
        "requested_route_id": "auto/coding",
        "selected_provider": "databricks",
        "selected_model": "databricks-claude-sonnet-4-6",
        "reasoning_effort": "medium",
        "permission_mode": "ask_before_edits",
        "omniroute_decision_id": "dec-abc",
        "billing_class": "subscription",
        "fallback_used": False,
    }
    defaults.update(kwargs)
    return store.create_run(CreateTaskRunInput(**defaults)).id


def _create_proposal(store: TaskOutcomeStore):
    return store.create_routing_proposal(
        CreateRoutingProposalInput(
            conversation_id="c1",
            elicitation_id="route_1",
            user_message="x" * 5000,
            content_types=["input_text"],
            original_route_id="auto/coding",
            original_harness="opencode-native",
            original_reasoning_effort="medium",
            original_permission_mode="ask_before_edits",
            requires_explicit_approval=True,
            evaluator_provider="databricks",
            evaluator_model="haiku",
            evaluator_decision_id="eval-1",
            proposal_payload={"route": "auto/coding", "audit": "y" * 5000},
        )
    )


def test_routing_proposal_is_bounded_and_hashed(store: TaskOutcomeStore) -> None:
    proposal = _create_proposal(store)
    assert len(proposal.user_message_excerpt) == 4096
    assert len(proposal.proposal_payload_excerpt) == 4096
    assert len(proposal.user_message_sha256) == 64
    assert proposal.evaluator_decision_id == "eval-1"


def test_routing_decision_is_idempotent_and_conflicts_fail(store: TaskOutcomeStore) -> None:
    proposal = _create_proposal(store)
    request = CreateRoutingDecisionInput(
        proposal_id=proposal.id,
        action="approved",
        decision_payload={"action": "accept"},
        final_harness="opencode-native",
        final_route_id="auto/coding",
        final_reasoning_effort="medium",
    )
    first = store.create_routing_decision(request)
    assert store.create_routing_decision(request).id == first.id
    with pytest.raises(RoutingDecisionConflictError):
        store.create_routing_decision(
            CreateRoutingDecisionInput(
                proposal_id=proposal.id,
                action="declined",
                decision_payload={"action": "decline"},
            )
        )


def test_task_run_links_exact_routing_decision(store: TaskOutcomeStore) -> None:
    proposal = _create_proposal(store)
    decision = store.create_routing_decision(
        CreateRoutingDecisionInput(
            proposal_id=proposal.id,
            action="approved",
            decision_payload={"action": "accept"},
            final_route_id="auto/coding",
        )
    )
    run_id = _create_run(
        store,
        routing_proposal_id=proposal.id,
        routing_decision_id=decision.id,
    )
    run = store.get_run(run_id)
    assert run is not None
    assert run.routing_proposal_id == proposal.id
    assert run.routing_decision_id == decision.id
    assert run.response_id == "r1"


def test_list_routing_turns_includes_decision_and_linked_run(store: TaskOutcomeStore) -> None:
    proposal = _create_proposal(store)
    decision = store.create_routing_decision(
        CreateRoutingDecisionInput(
            proposal_id=proposal.id,
            action="approved",
            decision_payload={"action": "accept"},
            final_route_id="auto/coding",
        )
    )
    run_id = _create_run(
        store,
        triggering_message_id="msg_user",
        routing_proposal_id=proposal.id,
        routing_decision_id=decision.id,
    )

    turns = store.list_routing_turns_for_conversation("c1")

    assert len(turns) == 1
    assert turns[0].proposal.id == proposal.id
    assert turns[0].decision == decision
    assert turns[0].task_run is not None
    assert turns[0].task_run.id == run_id
    assert store.list_routing_turns_for_conversation("c2") == []


def test_create_run_returns_row(store: TaskOutcomeStore) -> None:
    """``create_run`` returns a fully populated :class:`TaskRun`."""
    run_id = _create_run(store)
    run = store.get_run(run_id)
    assert run is not None
    assert run.conversation_id == "c1"
    assert run.terminal_status == "running"
    assert run.response_id == "r1"
    assert run.task_description == "Fix the login bug"
    assert run.requested_route_id == "auto/coding"
    assert run.selected_provider == "databricks"
    assert run.selected_model == "databricks-claude-sonnet-4-6"
    assert run.reasoning_effort == "medium"
    assert run.permission_mode == "ask_before_edits"
    assert run.omniroute_decision_id == "dec-abc"
    assert run.billing_class == "subscription"
    assert run.fallback_used is False
    assert run.started_at is not None
    assert run.created_at == run.started_at


def test_get_run_returns_none_when_missing(store: TaskOutcomeStore) -> None:
    """Missing run ids return ``None`` rather than raising."""
    assert store.get_run("nonexistent") is None


def test_get_run_for_conversation_scopes_by_owner(store: TaskOutcomeStore) -> None:
    """``get_run_for_conversation`` returns ``None`` for cross-session lookups."""
    run_id = _create_run(store, conv="c1")
    assert store.get_run_for_conversation(run_id, "c1") is not None
    assert store.get_run_for_conversation(run_id, "c2") is None


def test_update_run_terminal_computes_duration(store: TaskOutcomeStore) -> None:
    """``update_run_terminal`` derives ``duration_ms`` from started_at + terminal_at."""
    run_id = _create_run(store)
    # Use the run's actual started_at + a 5s offset so the
    # duration is positive regardless of wall-clock skew.
    started = store.get_run(run_id).started_at
    terminal_at = (started or 0) + 5
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run_id,
            terminal_status="completed",
            terminal_at=terminal_at,
            input_tokens=1000,
            output_tokens=200,
            total_cost_usd=0.01,
            response_summary="Fixed the login bug.",
            changed_files=["src/auth.py"],
            commit_sha="abc1234567",
        )
    )
    run = store.get_run(run_id)
    assert run is not None
    assert run.terminal_status == "completed"
    assert run.terminal_at == terminal_at
    assert run.duration_ms == 5000
    assert run.input_tokens == 1000
    assert run.output_tokens == 200
    assert run.total_cost_usd == 0.01
    assert run.response_summary == "Fixed the login bug."
    assert run.changed_files == ["src/auth.py"]
    assert run.commit_sha == "abc1234567"


def test_update_run_terminal_returns_none_when_missing(
    store: TaskOutcomeStore,
) -> None:
    """Unknown id is a no-op that returns ``None``."""
    assert (
        store.update_run_terminal(
            UpdateTaskRunTerminalInput(
                task_run_id="nonexistent",
                terminal_status="completed",
                terminal_at=200,
            )
        )
        is None
    )


def test_create_evaluation_persists_and_reads_back(
    store: TaskOutcomeStore,
) -> None:
    """``create_evaluation`` writes + reads a :class:`TaskEvaluation` row."""
    run_id = _create_run(store)
    eval_id = store.create_evaluation(
        CreateTaskEvaluationInput(
            task_run_id=run_id,
            evaluator_type="llm",
            evaluator_provider="databricks",
            evaluator_model="databricks-claude-sonnet-4-6",
            evaluator_route_id="auto/coding",
            verdict="success",
            confidence=0.85,
            quality_score=4,
            proposed_task_family="small_bug_fix",
            reasoning="Tests pass.",
            evidence=["unit tests green"],
            unresolved_issues=["minor: missing docstring"],
        )
    ).id
    eval_row = store.get_evaluation_for_run(run_id)
    assert eval_row is not None
    assert eval_row.id == eval_id
    assert eval_row.evaluator_type == "llm"
    assert eval_row.verdict == "success"
    assert eval_row.confidence == 0.85
    assert eval_row.quality_score == 4
    assert eval_row.proposed_task_family == "small_bug_fix"
    assert eval_row.evidence == ["unit tests green"]
    assert eval_row.unresolved_issues == ["minor: missing docstring"]


def test_create_evaluation_inconclusive_is_persisted(
    store: TaskOutcomeStore,
) -> None:
    """A failed-evaluator call lands as a single ``inconclusive`` row.

    Schema invariant: the review-card UI can rely on "always
    exactly one evaluation per task run" — the store records the
    failure as a verdict='inconclusive' row rather than dropping
    it, so the UI's LEFT JOIN logic stays simple.
    """
    run_id = _create_run(store)
    store.create_evaluation(
        CreateTaskEvaluationInput(
            task_run_id=run_id,
            evaluator_type="llm",
            verdict="inconclusive",
            reasoning="LLM call failed (timeout).",
        )
    )
    eval_row = store.get_evaluation_for_run(run_id)
    assert eval_row is not None
    assert eval_row.verdict == "inconclusive"
    assert eval_row.reasoning == "LLM call failed (timeout)."


def test_upsert_review_creates_then_updates_in_place(
    store: TaskOutcomeStore,
) -> None:
    """Re-submitting a review UPDATEs the existing row, doesn't append."""
    run_id = _create_run(store)
    initial = store.upsert_review(
        UpsertTaskReviewInput(
            task_run_id=run_id,
            verdict="success",
            quality_score=5,
            final_task_family="small_bug_fix",
            evaluator_accuracy="correct",
            comments="Looks great",
            created_by="alice@example.com",
        )
    )
    updated = store.upsert_review(
        UpsertTaskReviewInput(
            task_run_id=run_id,
            verdict="partial",
            quality_score=4,
            final_task_family="small_bug_fix",
            evaluator_accuracy="partly_correct",
            comments="Actually a partial fix.",
            created_by="alice@example.com",
        )
    )
    assert updated.id == initial.id, "upsert must UPDATE in place, not append"
    assert updated.verdict == "partial"
    assert updated.quality_score == 4
    assert updated.evaluator_accuracy == "partly_correct"
    assert updated.comments == "Actually a partial fix."


def test_upsert_review_separate_reviewers_get_separate_rows(
    store: TaskOutcomeStore,
) -> None:
    """Different ``created_by`` produces different rows (unique key scoping)."""
    run_id = _create_run(store)
    by_alice = store.upsert_review(
        UpsertTaskReviewInput(
            task_run_id=run_id,
            verdict="success",
            created_by="alice@example.com",
        )
    )
    by_bob = store.upsert_review(
        UpsertTaskReviewInput(
            task_run_id=run_id,
            verdict="partial",
            created_by="bob@example.com",
        )
    )
    assert by_alice.id != by_bob.id
    assert by_alice.created_by == "alice@example.com"
    assert by_bob.created_by == "bob@example.com"


def test_upsert_review_with_none_created_by(store: TaskOutcomeStore) -> None:
    """Single-user mode: ``created_by=None`` is allowed + idempotent."""
    run_id = _create_run(store)
    r1 = store.upsert_review(
        UpsertTaskReviewInput(task_run_id=run_id, verdict="success", created_by=None)
    )
    r2 = store.upsert_review(
        UpsertTaskReviewInput(task_run_id=run_id, verdict="skipped", created_by=None)
    )
    assert r1.id == r2.id
    assert r2.verdict == "skipped"
    assert r2.created_by is None


def test_get_review_for_run_isolates_by_created_by(
    store: TaskOutcomeStore,
) -> None:
    """``get_review_for_run`` only returns the requested reviewer's row."""
    run_id = _create_run(store)
    store.upsert_review(
        UpsertTaskReviewInput(
            task_run_id=run_id, verdict="success", created_by="alice@example.com"
        )
    )
    assert store.get_review_for_run(run_id, "alice@example.com") is not None
    assert store.get_review_for_run(run_id, "bob@example.com") is None


def test_list_runs_for_conversation_returns_both(
    store: TaskOutcomeStore,
) -> None:
    """``list_runs_for_conversation`` returns both runs; ordering is
    stable-by-id when started_at ties (same second)."""
    r1 = _create_run(store)
    r2 = _create_run(store, response_id="r2")
    runs = store.list_runs_for_conversation("c1")
    ids = {r.id for r in runs}
    assert r1 in ids and r2 in ids
    assert len(runs) == 2


def test_list_unreviewed_runs_excludes_reviewed(
    store: TaskOutcomeStore,
) -> None:
    """``list_unreviewed_runs`` only returns terminal runs with no review row."""
    r1 = _create_run(store, response_id="r1")
    r2 = _create_run(store, response_id="r2")
    r3 = _create_run(store, response_id="r3")
    # Terminalise all three.
    for rid in (r1, r2, r3):
        store.update_run_terminal(
            UpdateTaskRunTerminalInput(
                task_run_id=rid, terminal_status="completed", terminal_at=200
            )
        )
    # Review r2 only.
    store.upsert_review(
        UpsertTaskReviewInput(task_run_id=r2, verdict="success", created_by="alice")
    )
    # Skipped counts as reviewed (so it disappears from the unreviewed list).
    store.upsert_review(
        UpsertTaskReviewInput(task_run_id=r3, verdict="skipped", created_by="alice")
    )
    unreviewed = store.list_unreviewed_runs(conversation_id="c1")
    assert {r.id for r in unreviewed} == {r1}


def test_enqueue_langfuse_event_persists_pending_row(
    store: TaskOutcomeStore,
) -> None:
    """``enqueue_langfuse_event`` writes a ``pending`` outbox row."""
    run_id = _create_run(store)
    outbox_id = store.enqueue_langfuse_event(
        EnqueueLangfuseEventInput(
            task_run_id=run_id,
            event_type="task_root",
            idempotency_key=f"task:{run_id}:root:v1",
            payload={"id": "abc", "traceId": "def"},
        )
    ).id
    pending = store.claim_due_langfuse_events(now=10**12, limit=10)
    assert len(pending) == 1
    assert pending[0].id == outbox_id
    assert pending[0].status == "pending"
    assert pending[0].payload == {"id": "abc", "traceId": "def"}


def test_claim_due_langfuse_events_only_returns_due(
    store: TaskOutcomeStore,
) -> None:
    """``claim_due_langfuse_events`` skips rows whose ``next_attempt_at`` is in the future."""
    run_id = _create_run(store)
    store.enqueue_langfuse_event(
        EnqueueLangfuseEventInput(
            task_run_id=run_id,
            event_type="task_root",
            idempotency_key="k1",
            payload={"id": "a"},
            next_attempt_at=10**12,  # far future
        )
    )
    # Now=0 → the row's next_attempt_at is later, so nothing's due.
    assert store.claim_due_langfuse_events(now=0, limit=10) == []


def test_mark_langfuse_delivered_advances_state(
    store: TaskOutcomeStore,
) -> None:
    """``mark_langfuse_delivered`` flips status + records ``delivered_at``."""
    run_id = _create_run(store)
    outbox_id = store.enqueue_langfuse_event(
        EnqueueLangfuseEventInput(
            task_run_id=run_id,
            event_type="task_root",
            idempotency_key="k1",
            payload={"id": "a"},
        )
    ).id
    store.mark_langfuse_delivered(outbox_id, 12345)
    assert store.claim_due_langfuse_events(now=10**12, limit=10) == []
    # Verify the row's stored status via count_pending_langfuse_events.
    assert store.count_pending_langfuse_events(run_id) == 0


def test_mark_langfuse_failed_keeps_pending_and_bumps_attempt(
    store: TaskOutcomeStore,
) -> None:
    """``mark_langfuse_failed`` keeps ``status='pending'`` and bumps ``attempt_count``."""
    run_id = _create_run(store)
    outbox_id = store.enqueue_langfuse_event(
        EnqueueLangfuseEventInput(
            task_run_id=run_id,
            event_type="task_root",
            idempotency_key="k1",
            payload={"id": "a"},
        )
    ).id
    store.mark_langfuse_failed(outbox_id, last_error="HTTP 500", next_attempt_at=10**12)
    # Still pending, still due.
    due = store.claim_due_langfuse_events(now=10**12, limit=10)
    assert len(due) == 1
    assert due[0].attempt_count == 1
    assert due[0].last_error == "HTTP 500"


def test_mark_langfuse_dead_marks_terminal_failure(
    store: TaskOutcomeStore,
) -> None:
    """``mark_langfuse_dead`` flips ``status='dead'`` (terminal failure)."""
    run_id = _create_run(store)
    outbox_id = store.enqueue_langfuse_event(
        EnqueueLangfuseEventInput(
            task_run_id=run_id,
            event_type="task_root",
            idempotency_key="k1",
            payload={"id": "a"},
        )
    ).id
    store.mark_langfuse_dead(outbox_id, "exhausted")
    assert store.claim_due_langfuse_events(now=10**12, limit=10) == []
    assert store.count_pending_langfuse_events(run_id) == 0


def test_mark_langfuse_skipped_writes_audit_row(
    store: TaskOutcomeStore,
) -> None:
    """``mark_langfuse_skipped`` inserts a single ``skipped`` audit row."""
    run_id = _create_run(store)
    count = store.mark_langfuse_skipped(run_id)
    assert count == 1
    # Pending count is 0 (the audit row is skipped, not pending).
    assert store.count_pending_langfuse_events(run_id) == 0
    # The skipped row is still queryable for audit.
    assert store.claim_due_langfuse_events(now=10**12, limit=10) == []


def test_get_run_detail_aggregates(store: TaskOutcomeStore) -> None:
    """``get_run_detail`` returns the run + eval + (any) review + langfuse_pending flag."""
    run_id = _create_run(store)
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run_id, terminal_status="completed", terminal_at=200
        )
    )
    store.create_evaluation(
        CreateTaskEvaluationInput(task_run_id=run_id, evaluator_type="llm", verdict="success")
    )
    store.upsert_review(
        UpsertTaskReviewInput(task_run_id=run_id, verdict="success", created_by="alice")
    )
    store.enqueue_langfuse_event(
        EnqueueLangfuseEventInput(
            task_run_id=run_id,
            event_type="task_root",
            idempotency_key="k1",
            payload={"id": "a"},
        )
    )
    detail = store.get_run_detail(run_id)
    assert detail is not None
    assert detail.run.id == run_id
    assert detail.evaluation is not None and detail.evaluation.verdict == "success"
    assert detail.review is not None and detail.review.verdict == "success"
    assert detail.langfuse_pending is True


def test_set_langfuse_trace_ids_records_on_run(
    store: TaskOutcomeStore,
) -> None:
    """``set_langfuse_trace_ids`` stamps trace + observation ids onto the run."""
    run_id = _create_run(store)
    store.set_langfuse_trace_ids(run_id, "trace-xyz", "obs-abc")
    run = store.get_run(run_id)
    assert run is not None
    assert run.langfuse_trace_id == "trace-xyz"
    assert run.langfuse_observation_id == "obs-abc"


def test_changed_files_round_trip(store: TaskOutcomeStore) -> None:
    """``changed_files`` serialises to a JSON list and decodes back."""
    run_id = _create_run(store)
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run_id,
            terminal_status="completed",
            terminal_at=200,
            changed_files=["src/auth.py", "tests/test_auth.py"],
        )
    )
    run = store.get_run(run_id)
    assert run is not None
    assert run.changed_files == ["src/auth.py", "tests/test_auth.py"]


def test_evidence_json_round_trip(store: TaskOutcomeStore) -> None:
    """``evidence`` + ``unresolved_issues`` round-trip as JSON lists."""
    run_id = _create_run(store)
    store.create_evaluation(
        CreateTaskEvaluationInput(
            task_run_id=run_id,
            evaluator_type="llm",
            verdict="partial",
            evidence=["tests pass", "lint clean"],
            unresolved_issues=["commit message could be clearer"],
        )
    )
    eval_row = store.get_evaluation_for_run(run_id)
    assert eval_row is not None
    assert eval_row.evidence == ["tests pass", "lint clean"]
    assert eval_row.unresolved_issues == ["commit message could be clearer"]


def test_update_run_terminal_preserves_unset_fields(
    store: TaskOutcomeStore,
) -> None:
    """Fields not set on the update input are preserved from the original row."""
    run_id = _create_run(store)
    # First terminal with usage + summary.
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run_id,
            terminal_status="completed",
            terminal_at=200,
            input_tokens=500,
            output_tokens=100,
            response_summary="First pass summary.",
        )
    )
    # Second terminal with only status + terminal_at (e.g. a
    # corrected retry that doesn't carry new usage data).
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run_id,
            terminal_status="completed",
            terminal_at=300,
        )
    )
    run = store.get_run(run_id)
    assert run is not None
    # Original usage + summary preserved.
    assert run.input_tokens == 500
    assert run.output_tokens == 100
    assert run.response_summary == "First pass summary."
    # terminal_at updated.
    assert run.terminal_at == 300


def test_reviewer_unique_constraint_rejects_duplicate(
    store: TaskOutcomeStore,
) -> None:
    """``uq_task_reviews_run_reviewer`` rejects a second row for the same (run, reviewer)."""
    import sqlalchemy.exc

    from omnigent.db.utils import get_or_create_engine

    run_id = _create_run(store)
    store.upsert_review(
        UpsertTaskReviewInput(task_run_id=run_id, verdict="success", created_by="alice")
    )
    # Direct SQL insert bypassing upsert must hit the unique constraint.
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with get_or_create_engine(store.storage_location).begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO task_reviews (workspace_id, id, task_run_id, "
                    "verdict, created_by, created_at, updated_at) "
                    "VALUES (0, 'trv-dup', :run_id, 'success', 'alice', 200, 200)"
                ),
                {"run_id": run_id},
            )
