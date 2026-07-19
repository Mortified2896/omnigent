from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

import omnigent.db
from omnigent.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    uri = f"sqlite:///{tmp_path / 'routing.db'}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_routing_audit_tables_and_task_run_links(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    assert {"routing_proposals", "routing_decisions"} <= set(inspector.get_table_names())

    conversation_columns = {
        column["name"]: column for column in inspector.get_columns("conversations")
    }
    assert conversation_columns["routing_selection_source"]["nullable"] is True

    run_columns = {column["name"]: column for column in inspector.get_columns("task_runs")}
    assert run_columns["routing_proposal_id"]["nullable"] is True
    assert run_columns["routing_decision_id"]["nullable"] is True

    # SQLite adds these nullable linkage columns in place because rebuilding
    # task_runs would violate its historical evaluation/review references.
    if db_engine.dialect.name != "sqlite":
        run_fks = {fk["name"]: fk for fk in inspector.get_foreign_keys("task_runs")}
        assert run_fks["fk_task_runs_routing_proposal"]["referred_table"] == "routing_proposals"
        assert run_fks["fk_task_runs_routing_decision"]["referred_table"] == "routing_decisions"


def test_routing_decision_is_unique_per_proposal(db_engine: Engine) -> None:
    constraints = sa.inspect(db_engine).get_unique_constraints("routing_decisions")
    assert any(
        constraint["name"] == "uq_routing_decisions_proposal"
        and constraint["column_names"] == ["workspace_id", "proposal_id"]
        for constraint in constraints
    )


def test_upgrade_from_z9_preserves_historical_outcome_rows(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'historical.db'}"
    config = Config()
    config.set_main_option(
        "script_location", str(Path(omnigent.db.__file__).parent / "migrations")
    )
    config.set_main_option("sqlalchemy.url", uri)
    command.upgrade(config, "z9a2b3c4d5e6")
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO conversations (workspace_id, id, created_at, updated_at, "
                    "kind, root_conversation_id) VALUES (0, 'c_hist', 1, 1, 1, 'c_hist')"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO task_runs (workspace_id, id, conversation_id, response_id, "
                    "terminal_status, execution_status, evaluation_status, started_at, "
                    "created_at, updated_at) VALUES "
                    "(0, 'tr_hist', 'c_hist', 'resp_hist', 2, 'completed', "
                    "'completed', 10, 10, 20)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO task_evaluations (workspace_id, id, task_run_id, "
                    "evaluator_type, verdict, reasoning, created_at) VALUES "
                    "(0, 'tev_hist', 'tr_hist', 2, 'success', 'historical evaluation', 21)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO task_reviews (workspace_id, id, task_run_id, verdict, "
                    "comments, created_by, review_action, learning_eligible, created_at, "
                    "updated_at) VALUES (0, 'trv_hist', 'tr_hist', 'success', "
                    "'historical review', 'alice@example.com', 'accepted', 1, 22, 22)"
                )
            )

        command.upgrade(config, "head")

        with engine.connect() as conn:
            run = conn.execute(
                sa.text(
                    "SELECT response_id, routing_proposal_id, routing_decision_id "
                    "FROM task_runs WHERE id = 'tr_hist'"
                )
            ).one()
            selection_source = conn.execute(
                sa.text("SELECT routing_selection_source FROM conversations WHERE id = 'c_hist'")
            ).scalar_one()
            evaluation = conn.execute(
                sa.text("SELECT verdict, reasoning FROM task_evaluations WHERE id = 'tev_hist'")
            ).one()
            review = conn.execute(
                sa.text("SELECT verdict, comments FROM task_reviews WHERE id = 'trv_hist'")
            ).one()
        assert run == ("resp_hist", None, None)
        assert selection_source is None
        assert evaluation == ("success", "historical evaluation")
        assert review == ("success", "historical review")
    finally:
        engine.dispose()
