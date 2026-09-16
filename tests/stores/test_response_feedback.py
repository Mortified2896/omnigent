"""Durable, caller-scoped feedback and target validation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect, update

from omnigent.db.db_models import SqlConversationItem
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store import InvalidFeedbackTargetError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def answer(store, conversation_id, response_id="response", role="assistant", **kwargs):
    return store.append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role=role,
                    agent="test-agent" if role == "assistant" else None,
                    content=[{"type": "output_text", "text": "Answer"}],
                    **kwargs,
                ),
            )
        ],
    )[0]


def test_round_trip_and_idempotency(conversation_store):
    store = conversation_store
    conv = store.create_conversation()
    answer(store, conv.id)
    first = store.put_response_feedback(conv.id, "response", "local", 1)
    assert store.put_response_feedback(conv.id, "response", "local", 1) == first
    edited = store.put_response_feedback(
        conv.id, "response", "local", 1, comment="Useful", update_comment=True
    )
    switched = store.put_response_feedback(conv.id, "response", "local", -1)
    assert switched.comment == "Useful"
    assert switched.created_at == first.created_at
    assert switched.updated_at > edited.updated_at
    reopened = SqlAlchemyConversationStore(store.storage_location)
    assert reopened.list_response_feedback(conv.id, "local") == [switched]
    cleared = store.put_response_feedback(
        conv.id, "response", "local", -1, comment=None, update_comment=True
    )
    assert cleared.comment is None
    store.delete_response_feedback(conv.id, "response", "local")
    store.delete_response_feedback(conv.id, "response", "local")
    assert store.list_response_feedback(conv.id, "local") == []


def test_caller_isolation_and_concurrency(conversation_store):
    store = conversation_store
    conv = store.create_conversation()
    answer(store, conv.id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: store.put_response_feedback(conv.id, "response", "alice", 1), range(8)
            )
        )
    store.put_response_feedback(conv.id, "response", "bob", -1)
    assert len(store.list_response_feedback(conv.id, "alice")) == 1
    assert store.list_response_feedback(conv.id, "bob")[0].rating == -1
    store.delete_response_feedback(conv.id, "response", "alice")
    assert len(store.list_response_feedback(conv.id, "bob")) == 1


@pytest.mark.parametrize(
    "target", ["wrong_session", "user", "incomplete", "meta", "interrupted", "missing"]
)
def test_reject_invalid_targets(conversation_store, target):
    store = conversation_store
    conv = store.create_conversation()
    owner = store.create_conversation() if target == "wrong_session" else conv
    if target != "missing":
        answer(
            store,
            owner.id,
            role="user" if target == "user" else "assistant",
            is_meta=target == "meta",
            interrupted=target == "interrupted",
        )
    if target == "incomplete":
        with store._conv_session("test_mark_incomplete") as session:
            session.execute(update(SqlConversationItem).values(status=2))
    with pytest.raises(InvalidFeedbackTargetError):
        store.put_response_feedback(conv.id, "response", "local", 1)
    with pytest.raises(InvalidFeedbackTargetError):
        store.delete_response_feedback(conv.id, "response", "local")
    assert store.list_response_feedback(conv.id, "local") == []


def test_split_database_and_cleanup(tmp_path):
    store = SqlAlchemyConversationStore(
        f"sqlite:///{tmp_path}/ops.db", f"sqlite:///{tmp_path}/conv.db"
    )
    conv = store.create_conversation()
    answer(store, conv.id)
    store.put_response_feedback(conv.id, "response", "local", 1)
    assert "response_feedback" in inspect(store._conv_engine).get_table_names()
    assert len(store.list_response_feedback(conv.id, "local")) == 1
    asyncio.run(store.delete_conversation(conv.id))
    assert store.list_response_feedback(conv.id, "local") == []


@pytest.mark.parametrize("rating", [0, 2, True])
def test_rating_contract(conversation_store, rating):
    with pytest.raises(ValueError):
        conversation_store.put_response_feedback("unused", "response", "local", rating)


def test_migration_upgrade_downgrade(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine

    from omnigent.db.utils import _build_alembic_config

    uri = f"sqlite:///{tmp_path}/migration.db"
    config = _build_alembic_config(uri)
    command.upgrade(config, "ge1b2c3d4e5f")
    engine = create_engine(uri)
    assert "response_feedback" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    table = inspect(engine)
    assert table.get_pk_constraint("response_feedback")["constrained_columns"] == [
        "workspace_id",
        "conversation_id",
        "response_id",
        "user_id",
    ]
    assert table.get_check_constraints("response_feedback")
    command.downgrade(config, "ge1b2c3d4e5f")
    assert "response_feedback" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    assert "response_feedback" in inspect(engine).get_table_names()
    engine.dispose()


def test_workspace_isolation(conversation_store):
    from omnigent.db.db_models import workspace_scope

    store = conversation_store
    conv = store.create_conversation()
    answer(store, conv.id)
    store.put_response_feedback(conv.id, "response", "alice", 1)
    with workspace_scope(7):
        assert store.list_response_feedback(conv.id, "alice") == []
        with pytest.raises(InvalidFeedbackTargetError):
            store.put_response_feedback(conv.id, "response", "alice", -1)
    assert store.list_response_feedback(conv.id, "alice")[0].rating == 1


def test_database_rejects_invalid_rating(conversation_store):
    from sqlalchemy.exc import IntegrityError

    from omnigent.db.db_models import SqlResponseFeedback

    store = conversation_store
    conv = store.create_conversation()
    answer(store, conv.id)
    store.put_response_feedback(conv.id, "response", "local", 1)
    with pytest.raises(IntegrityError), store._conv_session("test_rating_constraint") as session:
        session.execute(update(SqlResponseFeedback).values(rating=0))
