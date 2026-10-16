"""Durable per-viewer read receipts in the existing preferences table."""

from __future__ import annotations

import json
import time

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.server.auth import RESERVED_USER_LOCAL

ReadState = tuple[int | None, bool, int]
_PREFIX = "session_read:"


class SessionReadStateStore:
    """Share read receipts across workers and retain them across restarts."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.session_read_state"
        )
        self._write_session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.session_read_state", immediate=True
        )

    def get_many(self, user_id: str | None, session_ids: list[str]) -> dict[str, ReadState]:
        """Batch-read only this viewer's receipts in the current workspace."""
        if not session_ids:
            return {}
        with self._session("get_many") as session:
            rows = session.execute(
                select(SqlPreference.key, SqlPreference.value).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == (user_id or RESERVED_USER_LOCAL),
                    SqlPreference.key.in_([_PREFIX + sid for sid in session_ids]),
                )
            )
            return {
                key.removeprefix(_PREFIX): (state[0], state[1], state[2])
                for key, value in rows
                if isinstance(state := json.loads(value), list) and len(state) == 3
            }

    def put(self, user_id: str | None, session_id: str, last_seen: int, unread: bool) -> int:
        """Serialize updates; delayed ordinary receipts cannot move a read backwards."""

        def write(session: Session) -> int:
            require_active_account(session, user_id)
            workspace_id = current_workspace_id()
            owner = user_id or RESERVED_USER_LOCAL
            key = _PREFIX + session_id
            values = {
                "workspace_id": workspace_id,
                "user_id": owner,
                "key": key,
                "value": "[null,false,0]",
            }
            # Reserve the row before locking it, including concurrent first writes.
            dialect = self._engine.dialect.name
            if dialect == "mysql":
                statement = (
                    mysql_insert(SqlPreference).values(**values).on_duplicate_key_update(key=key)
                )
            else:
                insert = sqlite_insert if dialect in ("sqlite", "cloudflare_d1") else pg_insert
                statement = insert(SqlPreference).values(**values).on_conflict_do_nothing()
            session.execute(statement)
            row = session.scalars(
                select(SqlPreference)
                .where(
                    SqlPreference.workspace_id == workspace_id,
                    SqlPreference.user_id == owner,
                    SqlPreference.key == key,
                )
                .with_for_update()
            ).one()
            previous_seen, previous_unread, version = json.loads(row.value)
            if not unread and not previous_unread and previous_seen is not None:
                last_seen_value = max(last_seen, previous_seen)
            else:
                last_seen_value = last_seen
            if (last_seen_value, unread) != (previous_seen, previous_unread):
                # Stay newer than browser receipts retained through archive/delete.
                version = max(time.time_ns() // 1_000_000, version + 1)
                row.value = json.dumps([last_seen_value, unread, version])
            return version

        return run_write_transaction(self._write_session, "put", write)

    def prune(self, session_id: str) -> None:
        """Remove only this session's receipts, for all viewers of this workspace."""

        def write(session: Session) -> None:
            session.execute(
                delete(SqlPreference).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.key == _PREFIX + session_id,
                )
            )

        run_write_transaction(self._write_session, "prune", write)
