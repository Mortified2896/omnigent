"""Startup accepts known branch heads while preserving the fork ancestry."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from omnigent.db.utils import (
    _build_alembic_config,
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
    _run_migrations,
)


def test_startup_rejoins_partial_upstream_downgrade(tmp_path):
    uri = f"sqlite:///{tmp_path / 'joined.sqlite'}"
    engine = create_engine(uri)
    try:
        _run_migrations(engine, uri)
        command.downgrade(_build_alembic_config(uri), "ii1a2b3c4d5e")
        assert set(_get_current_db_revision(engine)) == {"ii1a2b3c4d5e", "c91f6a2d7e40"}
        _initialize_or_verify_schema(engine, uri)
        assert _get_current_db_revision(engine) == _get_head_db_revision(uri)
    finally:
        engine.dispose()


def test_unknown_branch_head_refuses_startup_without_mutation(tmp_path):
    uri = f"sqlite:///{tmp_path / 'unknown.sqlite'}"
    engine = create_engine(uri)
    try:
        _run_migrations(engine, uri)
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO alembic_version VALUES ('unknown_future')"))
        before = _get_current_db_revision(engine)
        with pytest.raises(RuntimeError, match="newer than this version"):
            _initialize_or_verify_schema(engine, uri)
        assert _get_current_db_revision(engine) == before
    finally:
        engine.dispose()
