"""Regression tests for duplicate live host identity protection."""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlHost
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.server.routes.host_tunnel import _find_live_host_name_collision
from omnigent.stores.host_store import HOST_LIVENESS_TTL_S, HostStore


_EXISTING_HOST_ID = "ed5ddb6695a84c3091d8925b7394a6c4"
_CONNECTING_HOST_ID = "07e100efd05744b593285539f0e985ec"


def _host_store(db_uri: str) -> HostStore:
    return HostStore(db_uri)


def _register_existing(host_store: HostStore, *, name: str = "ai-control-hub") -> None:
    host_store.upsert_on_connect(
        host_id=_EXISTING_HOST_ID,
        name=name,
        user_id="local",
    )


def test_rejects_different_live_host_with_same_owner_and_name(db_uri: str) -> None:
    host_store = _host_store(db_uri)
    _register_existing(host_store)

    collision = _find_live_host_name_collision(
        host_store,
        host_id=_CONNECTING_HOST_ID,
        user_id="local",
        name="ai-control-hub",
    )

    assert collision is not None
    assert collision.host_id == _EXISTING_HOST_ID


def test_allows_reconnect_with_same_host_id(db_uri: str) -> None:
    host_store = _host_store(db_uri)
    _register_existing(host_store)

    collision = _find_live_host_name_collision(
        host_store,
        host_id=_EXISTING_HOST_ID,
        user_id="local",
        name="ai-control-hub",
    )

    assert collision is None


def test_allows_rotation_when_previous_host_is_offline(db_uri: str) -> None:
    host_store = _host_store(db_uri)
    _register_existing(host_store)
    host_store.set_offline(_EXISTING_HOST_ID)

    collision = _find_live_host_name_collision(
        host_store,
        host_id=_CONNECTING_HOST_ID,
        user_id="local",
        name="ai-control-hub",
    )

    assert collision is None


def test_allows_rotation_when_previous_host_heartbeat_is_stale(db_uri: str) -> None:
    host_store = _host_store(db_uri)
    _register_existing(host_store)
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlHost)
            .where(SqlHost.host_id == _EXISTING_HOST_ID)
            .values(updated_at=now_epoch() - HOST_LIVENESS_TTL_S - 1)
        )
        session.commit()

    collision = _find_live_host_name_collision(
        host_store,
        host_id=_CONNECTING_HOST_ID,
        user_id="local",
        name="ai-control-hub",
    )

    assert collision is None


def test_allows_distinct_live_host_name(db_uri: str) -> None:
    host_store = _host_store(db_uri)
    _register_existing(host_store, name="maintenance-host")

    collision = _find_live_host_name_collision(
        host_store,
        host_id=_CONNECTING_HOST_ID,
        user_id="local",
        name="ai-control-hub",
    )

    assert collision is None
