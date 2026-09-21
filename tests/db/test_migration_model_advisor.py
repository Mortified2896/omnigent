"""Migration coverage for the model advisor record store.

The advisor table must come from the normal Alembic chain (never runtime
create_all), survive the SQLite-safe round trip, and match the schema the
``AdvisorRepository`` expects.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path):
    uri = f"sqlite:///{tmp_path}/advisor-migration.db"
    engine = get_or_create_engine(uri)
    yield engine
    clear_engine_cache()


def _table_names(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_migration_creates_model_advisor_records(db_engine: sa.Engine) -> None:
    columns = {
        column["name"]: column
        for column in sa.inspect(db_engine).get_columns("model_advisor_records")
    }
    assert set(columns) == {"scope_key", "version", "state", "payload"}
    assert columns["scope_key"]["primary_key"]
    assert not columns["version"]["nullable"]
    assert not columns["state"]["nullable"]
    assert not columns["payload"]["nullable"]
    pk = sa.inspect(db_engine).get_pk_constraint("model_advisor_records")
    assert pk["constrained_columns"] == ["scope_key"]


def test_repository_roundtrip_on_migrated_schema(db_engine: sa.Engine) -> None:
    """The migrated table serves the repository's real CAS operations."""
    from omnigent.model_advisor_repository import AdvisorConflict, AdvisorRepository
    from omnigent.model_advisor_workflow import AdvisorPreferences

    repository = AdvisorRepository(db_engine)
    preferences = AdvisorPreferences(
        enabled=True,
        allowed_candidate_ids=("choice-1",),
        advisor_candidate_id="choice-2",
        human_probability_percent=50,
    )
    saved = repository.save_preferences("owner@test", "host_1", preferences, expected_version=0)
    assert saved.version == 1
    reloaded = repository.load_preferences("owner@test", "host_1")
    assert reloaded is not None and reloaded.version == 1
    with pytest.raises(AdvisorConflict):
        repository.save_preferences("owner@test", "host_1", preferences, expected_version=0)


def test_sqlite_safe_roundtrip_includes_advisor_table(tmp_path) -> None:
    """Upgrading past the advisor revision and back down keeps the chain sane."""
    uri = f"sqlite:///{tmp_path}/advisor-roundtrip.db"
    engine = get_or_create_engine(uri)
    try:
        assert "model_advisor_records" in _table_names(engine)
        config = _build_alembic_config(uri)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "f8a9b0c1d2e3")
            assert "model_advisor_records" not in _table_names(connection)
            command.upgrade(config, "head")
            assert "model_advisor_records" in _table_names(connection)
    finally:
        clear_engine_cache()
