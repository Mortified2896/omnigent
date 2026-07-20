"""Migration tests for durable MiniMax-M3 evaluation lifecycle metadata."""

from __future__ import annotations

from sqlalchemy import inspect, text

from omnigent.db.utils import get_or_create_engine


def test_lifecycle_schema_and_historical_treatment(tmp_path) -> None:
    uri = f"sqlite:///{tmp_path / 'lifecycle.db'}"
    engine = get_or_create_engine(uri)
    columns = {column["name"] for column in inspect(engine).get_columns("task_runs")}
    assert {
        "evaluation_attempt_count",
        "evaluation_last_attempt_at",
        "evaluation_next_retry_at",
        "evaluation_error_kind",
        "evaluation_error_code",
        "evaluation_error_message",
        "evaluation_requested_model",
    } <= columns
    evaluation_columns = {
        column["name"] for column in inspect(engine).get_columns("task_evaluations")
    }
    assert {"evaluator_fallback_used", "evaluator_decision_id"} <= evaluation_columns
    uniques = inspect(engine).get_unique_constraints("task_evaluations")
    assert any(
        constraint["name"] == "uq_task_evaluations_run"
        and constraint["column_names"] == ["workspace_id", "task_run_id"]
        for constraint in uniques
    )
    with engine.connect() as connection:
        assert connection.execute(text("select version_num from alembic_version")).scalar() == (
            "zb1b2c3d4e5f"
        )
