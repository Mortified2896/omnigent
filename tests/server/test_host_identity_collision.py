"""Regression coverage for duplicate live host identity protection."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlHost
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.server.host_identity import find_live_host_name_collision
from omnigent.stores.host_store import HOST_LIVENESS_TTL_S, HostStore

_EXISTING_HOST_ID = "ed5ddb6695a84c3091d8925b7394a6c4"
_CONNECTING_HOST_ID = "07e100efd05744b593285539f0e985ec"
_OWNER = "local"
_NAME = "ai-control-hub"


def _store(tmp_path: Path) -> HostStore:
    return HostStore(f"sqlite:///{tmp_path / 'hosts.db'}")


def _register_existing(host_store: HostStore, *, name: str = _NAME) -> None:
    host_store.upsert_on_connect(
        host_id=_EXISTING_HOST_ID,
        name=name,
        user_id=_OWNER,
    )


def _collision(host_store: HostStore, *, host_id: str = _CONNECTING_HOST_ID):
    return find_live_host_name_collision(
        host_store,
        host_id=host_id,
        user_id=_OWNER,
        name=_NAME,
    )


def test_rejects_different_live_host_with_same_owner_and_name(tmp_path: Path) -> None:
    host_store = _store(tmp_path)
    _register_existing(host_store)

    collision = _collision(host_store)

    assert collision is not None
    assert collision.host_id == _EXISTING_HOST_ID


def test_allows_reconnect_with_same_host_id(tmp_path: Path) -> None:
    host_store = _store(tmp_path)
    _register_existing(host_store)

    assert _collision(host_store, host_id=_EXISTING_HOST_ID) is None


def test_allows_rotation_when_previous_host_is_offline(tmp_path: Path) -> None:
    host_store = _store(tmp_path)
    _register_existing(host_store)
    host_store.set_offline(_EXISTING_HOST_ID)

    assert _collision(host_store) is None


def test_allows_rotation_when_previous_heartbeat_is_stale(tmp_path: Path) -> None:
    host_store = _store(tmp_path)
    _register_existing(host_store)
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'hosts.db'}")
    with Session(engine) as session:
        session.execute(
            update(SqlHost)
            .where(SqlHost.host_id == _EXISTING_HOST_ID)
            .values(updated_at=now_epoch() - HOST_LIVENESS_TTL_S - 1)
        )
        session.commit()

    assert _collision(host_store) is None


def test_allows_same_name_for_a_different_owner(tmp_path: Path) -> None:
    host_store = _store(tmp_path)
    _register_existing(host_store)

    assert (
        find_live_host_name_collision(
            host_store,
            host_id=_CONNECTING_HOST_ID,
            user_id="different-owner",
            name=_NAME,
        )
        is None
    )
