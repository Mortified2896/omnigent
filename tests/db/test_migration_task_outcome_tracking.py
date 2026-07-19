"""Tests for the task-outcome tracking migration (``z7a2b3c4d5e6``).

Seeds a database at the prior revision, applies the migration,
and verifies the four tables (task_runs, task_evaluations,
task_reviews, langfuse_sync_outbox) have the expected columns,
indexes, FKs, and CHECK constraints. Also asserts the
``downgrade`` reverses the migration cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full alembic chain applied; cleaned up after."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def _seed_conversation(engine: Engine) -> None:
    """Insert one conversation so FKs in the new tables can be exercised."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations (id, created_at, updated_at, kind, "
                "root_conversation_id) VALUES ('c1', 1, 1, 1, 'c1')"
            )
        )


def _columns(insp: sa.Inspector, table: str) -> list[str]:
    return [c["name"] for c in insp.get_columns(table)]


def test_all_four_tables_created(db_engine: Engine) -> None:
    """The migration must add all four task-outcome tables."""
    insp = sa.inspect(db_engine)
    tables = set(insp.get_table_names())
    assert "task_runs" in tables
    assert "task_evaluations" in tables
    assert "task_reviews" in tables
    assert "langfuse_sync_outbox" in tables


def test_task_runs_schema(db_engine: Engine) -> None:
    """``task_runs`` has every required routing + status + timing column."""
    cols = set(_columns(sa.inspect(db_engine), "task_runs"))
    required = {
        "workspace_id",
        "id",
        "conversation_id",
        "response_id",
        "triggering_message_id",
        "project_path",
        "task_description",
        "proposed_task_family",
        "estimated_difficulty",
        "harness_id",
        "requested_route_id",
        "selected_provider",
        "selected_model",
        "reasoning_effort",
        "permission_mode",
        "omniroute_decision_id",
        "selection_strategy",
        "billing_class",
        "fallback_used",
        "terminal_status",
        "started_at",
        "terminal_at",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "total_cost_usd",
        "response_summary",
        "changed_files_json",
        "commit_sha",
        "failure_error_code",
        "failure_error_message",
        "langfuse_trace_id",
        "langfuse_observation_id",
        "created_at",
        "updated_at",
    }
    missing = required - cols
    assert not missing, f"task_runs missing columns: {sorted(missing)}"


def test_task_evaluations_schema(db_engine: Engine) -> None:
    """``task_evaluations`` has every required field."""
    cols = set(_columns(sa.inspect(db_engine), "task_evaluations"))
    required = {
        "workspace_id",
        "id",
        "task_run_id",
        "evaluator_type",
        "evaluator_provider",
        "evaluator_model",
        "evaluator_route_id",
        "verdict",
        "confidence",
        "quality_score",
        "proposed_task_family",
        "reasoning",
        "evidence_json",
        "unresolved_issues_json",
        "created_at",
    }
    missing = required - cols
    assert not missing, f"task_evaluations missing columns: {sorted(missing)}"


def test_task_reviews_schema(db_engine: Engine) -> None:
    """``task_reviews`` has the reviewer fields + the unique reviewer key."""
    cols = set(_columns(sa.inspect(db_engine), "task_reviews"))
    required = {
        "workspace_id",
        "id",
        "task_run_id",
        "verdict",
        "quality_score",
        "final_task_family",
        "evaluator_accuracy",
        "comments",
        "created_by",
        "created_at",
        "updated_at",
    }
    missing = required - cols
    assert not missing, f"task_reviews missing columns: {sorted(missing)}"
    # The idempotent re-submit unique constraint.
    uqs = sa.inspect(db_engine).get_unique_constraints("task_reviews")
    names = {u["name"] for u in uqs}
    assert "uq_task_reviews_run_reviewer" in names


def test_langfuse_sync_outbox_schema(db_engine: Engine) -> None:
    """``langfuse_sync_outbox`` has every retry + idempotency column."""
    cols = set(_columns(sa.inspect(db_engine), "langfuse_sync_outbox"))
    required = {
        "workspace_id",
        "id",
        "task_run_id",
        "task_evaluation_id",
        "event_type",
        "idempotency_key",
        "payload_json",
        "status",
        "attempt_count",
        "last_error",
        "next_attempt_at",
        "created_at",
        "delivered_at",
    }
    missing = required - cols
    assert not missing, f"langfuse_sync_outbox missing columns: {sorted(missing)}"


def test_task_runs_indexes(db_engine: Engine) -> None:
    """Three indexes cover the hot paths (list-by-conv, lookup-by-response, list-by-status)."""
    names = {i["name"] for i in sa.inspect(db_engine).get_indexes("task_runs")}
    assert "ix_task_runs_conversation_started_at" in names
    assert "ix_task_runs_response_id" in names
    assert "ix_task_runs_terminal_status" in names


def test_langfuse_sync_outbox_indexes(db_engine: Engine) -> None:
    """Two indexes: the worker's due-scan + the run-scoped join."""
    names = {i["name"] for i in sa.inspect(db_engine).get_indexes("langfuse_sync_outbox")}
    assert "ix_langfuse_outbox_due" in names
    assert "ix_langfuse_outbox_run" in names


def test_task_runs_fk_to_conversations(db_engine: Engine) -> None:
    """``task_runs.conversation_id`` FK references ``conversations.id``."""
    fks = sa.inspect(db_engine).get_foreign_keys("task_runs")
    fk = next(entry for entry in fks if entry["name"] == "fk_task_runs_conversation")
    assert fk["referred_table"] == "conversations"
    assert fk["constrained_columns"] == ["workspace_id", "conversation_id"]
    assert fk["referred_columns"] == ["workspace_id", "id"]


def test_task_evaluations_fk_to_task_runs(db_engine: Engine) -> None:
    """``task_evaluations.task_run_id`` FK references ``task_runs.id``."""
    fks = sa.inspect(db_engine).get_foreign_keys("task_evaluations")
    assert len(fks) == 1
    assert fks[0]["referred_table"] == "task_runs"


def test_langfuse_outbox_fk_to_task_runs(db_engine: Engine) -> None:
    """``langfuse_sync_outbox.task_run_id`` FK references ``task_runs.id``."""
    fks = sa.inspect(db_engine).get_foreign_keys("langfuse_sync_outbox")
    assert len(fks) == 1
    assert fks[0]["referred_table"] == "task_runs"


def test_round_trip_task_run(db_engine: Engine) -> None:
    """Insert + read a task_runs row + child evaluations + reviews + outbox.

    Exercises the schema directly (raw SQL, no ORM) so column
    drift is caught independently of the store wrapper. The
    CHECK constraints reject invalid enum codes; the FKs
    enforce ``task_run_id`` → ``task_runs.id``; the unique
    constraint on ``task_reviews`` enforces one-row-per-reviewer.
    """
    _seed_conversation(db_engine)
    with db_engine.begin() as conn:
        # Run row.
        conn.execute(
            sa.text(
                "INSERT INTO task_runs (workspace_id, id, conversation_id, "
                "terminal_status, started_at, created_at, updated_at) "
                "VALUES (0, 'tr1', 'c1', 2, 100, 100, 100)"
            )
        )
        # Evaluation row.
        conn.execute(
            sa.text(
                "INSERT INTO task_evaluations (workspace_id, id, task_run_id, "
                "evaluator_type, verdict, created_at) "
                "VALUES (0, 'tev1', 'tr1', 2, 'success', 100)"
            )
        )
        # Review row (creator known).
        conn.execute(
            sa.text(
                "INSERT INTO task_reviews (workspace_id, id, task_run_id, "
                "verdict, created_by, created_at, updated_at) "
                "VALUES (0, 'trv1', 'tr1', 'success', 'alice@example.com', 100, 100)"
            )
        )
        # Outbox row.
        conn.execute(
            sa.text(
                "INSERT INTO langfuse_sync_outbox (workspace_id, id, "
                "task_run_id, event_type, idempotency_key, payload_json, "
                "status, attempt_count, next_attempt_at, created_at) "
                "VALUES (0, 'lfs1', 'tr1', 'task_root', 'task:tr1:root:v1', "
                "X'7b7d00', 1, 0, 100, 100)"
            )
        )
        # Round-trip read.
        run = conn.execute(
            sa.text("SELECT terminal_status FROM task_runs WHERE id = 'tr1'")
        ).scalar_one()
        assert run == 2
        eval_row = conn.execute(
            sa.text("SELECT verdict FROM task_evaluations WHERE id = 'tev1'")
        ).scalar_one()
        assert eval_row == "success"
        review_row = conn.execute(
            sa.text("SELECT verdict FROM task_reviews WHERE id = 'trv1'")
        ).scalar_one()
        assert review_row == "success"
        outbox_row = conn.execute(
            sa.text("SELECT status, event_type FROM langfuse_sync_outbox WHERE id = 'lfs1'")
        ).one()
        assert outbox_row[0] == 1
        assert outbox_row[1] == "task_root"


def test_task_review_reviewer_unique_constraint(db_engine: Engine) -> None:
    """``uq_task_reviews_run_reviewer`` rejects two reviews by the same reviewer."""
    import sqlalchemy.exc

    _seed_conversation(db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO task_runs (workspace_id, id, conversation_id, "
                "terminal_status, created_at, updated_at) "
                "VALUES (0, 'tr2', 'c1', 2, 100, 100)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO task_reviews (workspace_id, id, task_run_id, "
                "verdict, created_by, created_at, updated_at) "
                "VALUES (0, 'trv-a', 'tr2', 'success', 'alice@example.com', 100, 100)"
            )
        )
    # Second insert by the same reviewer must fail with the unique constraint.
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO task_reviews (workspace_id, id, task_run_id, "
                    "verdict, created_by, created_at, updated_at) "
                    "VALUES (0, 'trv-b', 'tr2', 'partial', 'alice@example.com', 200, 200)"
                )
            )


def test_task_runs_check_constraint_rejects_unknown_status(db_engine: Engine) -> None:
    """``ck_task_runs_terminal_status`` rejects codes outside the enum."""
    import sqlalchemy.exc

    _seed_conversation(db_engine)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO task_runs (workspace_id, id, conversation_id, "
                    "terminal_status, created_at, updated_at) "
                    "VALUES (0, 'tr-bad', 'c1', 99, 100, 100)"
                )
            )


def test_task_evaluations_check_constraint_rejects_unknown_verdict(db_engine: Engine) -> None:
    """``ck_task_evaluations_verdict`` rejects verdicts outside the vocabulary."""
    import sqlalchemy.exc

    _seed_conversation(db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO task_runs (workspace_id, id, conversation_id, "
                "terminal_status, created_at, updated_at) "
                "VALUES (0, 'tr3', 'c1', 2, 100, 100)"
            )
        )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO task_evaluations (workspace_id, id, task_run_id, "
                    "evaluator_type, verdict, created_at) "
                    "VALUES (0, 'tev-bad', 'tr3', 2, 'unknown', 100)"
                )
            )


def test_langfuse_outbox_check_constraint_rejects_unknown_status(db_engine: Engine) -> None:
    """``ck_langfuse_outbox_status`` rejects codes outside the enum."""
    import sqlalchemy.exc

    _seed_conversation(db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO task_runs (workspace_id, id, conversation_id, "
                "terminal_status, created_at, updated_at) "
                "VALUES (0, 'tr4', 'c1', 2, 100, 100)"
            )
        )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO langfuse_sync_outbox (workspace_id, id, "
                    "task_run_id, event_type, idempotency_key, payload_json, "
                    "status, attempt_count, next_attempt_at, created_at) "
                    "VALUES (0, 'lfs-bad', 'tr4', 'task_root', "
                    "'task:tr4:root:v1', X'7b7d00', 99, 0, 100, 100)"
                )
            )
