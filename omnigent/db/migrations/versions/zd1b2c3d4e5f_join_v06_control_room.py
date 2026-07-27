"""Finish the stock v0.6 and Control Room schema on either lineage.

Fresh v0.6 databases reach ``e5c8b1f4a2d7``. Production Control Room
snapshots reach ``zc1b2c3d4e5f`` on the pre-v0.6 lineage. This migration is the
single deployment head: Alembic follows the Control Room branch on fresh
schemas, while this revision upgrades an existing production snapshot through
the stock post-z5 schema changes before normalising the Control Room audit
references to the same binary UUID representation as the core tables.
"""
from __future__ import annotations

import importlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zd1b2c3d4e5f"
down_revision: str | None = "zc1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The production lineage branched before these stock migrations. They are
# invoked only when their schema markers are absent; a fresh v0.6 database has
# already applied them before it walks the Control Room branch.
_STOCK_POST_Z5 = (
    "aa1b2c3d4e5f_split_conversations_to_metadata",
    "bb2c3d4e5f6a_split_agent_configuration_from_conversations",
    "cc3d4e5f6a7b_add_conversation_items_conv_type_position_index",
    "9d820f91deef_move_archived_to_conversations",
    "z6a2b3c4d5e6_add_scheduled_tasks_tables",
    "z7a2b3c4d5e6_convert_ids_to_binary_uuid",
    "z8a2b3c4d5e6_widen_conversation_items_pk_with_created_at",
    "a7b3c4d5e6f7_scheduled_tasks_cron_to_rrule",
    "d7f1a2b3c4e5_add_conversation_metadata_live_state",
    "d1e2f3a4b5c6_add_device_grants_table",
    "f4a1c8b2d3e6_drop_conversations_timestamp_indexes",
    "a2b7c3d8e4f9_conversations_title_hash_index",
    "c7d2e9f4a1b8_conversation_items_position_plain_index",
    "b7e4d2c9a1f3_merge_agent_configuration_into_conversations",
    "f6d3b8a2c1e9_drop_unused_conversation_metadata_kind_index",
    "e5c8b1f4a2d7_drop_unused_scheduled_tasks_state_index",
)

_CUSTOM_ID_COLUMNS: dict[str, tuple[str, ...]] = {
    "routing_proposals": ("id", "conversation_id"),
    "routing_decisions": ("id", "proposal_id"),
    "task_runs": (
        "id",
        "conversation_id",
        "triggering_message_id",
        "routing_proposal_id",
        "routing_decision_id",
    ),
    "task_evaluations": ("id", "task_run_id"),
    "task_reviews": ("id", "task_run_id", "source_evaluation_id"),
    "langfuse_sync_outbox": ("id", "task_run_id", "task_evaluation_id"),
}


def _table_exists(bind: sa.Connection, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _column_exists(bind: sa.Connection, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _stock_schema_is_present(bind: sa.Connection) -> bool:
    """Return true when the production copy already has the post-z5 core."""
    return _table_exists(bind, "scheduled_tasks") and _column_exists(
        bind, "conversations", "session_overrides"
    ) and any(
        str(c["type"]).upper().startswith("BLOB")
        for c in sa.inspect(bind).get_columns("conversations")
        if c["name"] == "id"
    )


def _apply_stock_post_z5() -> None:
    """Replay stock migrations against the legacy production schema."""
    for module_name in _STOCK_POST_Z5:
        module = importlib.import_module(
            f"omnigent.db.migrations.versions.{module_name}"
        )
        module.upgrade()


def _id_to_bytes(value: object) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) == 16:
            return raw
        value = raw.decode("ascii")
    text = str(value).replace("-", "")
    tail = text[-32:]
    if len(tail) == 32:
        try:
            return bytes.fromhex(tail)
        except ValueError:
            pass
    # Match the stock conversion's deterministic fallback for non-UUID host
    # and session identifiers so references remain consistent.
    import hashlib

    return hashlib.md5(str(value).encode("utf-8")).digest()


def _convert_custom_ids() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    op.execute(sa.text("PRAGMA foreign_keys = OFF"))
    try:
        for table, columns in _CUSTOM_ID_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            reflected = {c["name"]: c for c in inspector.get_columns(table)}
            target = [c for c in columns if c in reflected]
            text_columns = [
                c for c in target if not str(reflected[c]["type"]).upper().startswith("BLOB")
            ]
            if not text_columns:
                continue
            selected = ", ".join(f'"{c}"' for c in text_columns)
            rows = bind.execute(
                sa.text(f'SELECT rowid, {selected} FROM "{table}"')
            ).fetchall()
            for row in rows:
                values = {
                    col: _id_to_bytes(row[index])
                    for index, col in enumerate(text_columns, start=1)
                    if row[index] is not None
                }
                if not values:
                    continue
                assignments = ", ".join(f'"{c}" = :{c}' for c in values)
                bind.execute(
                    sa.text(
                        f'UPDATE "{table}" SET {assignments} WHERE rowid = :__rowid'
                    ),
                    {**values, "__rowid": row[0]},
                )
            with op.batch_alter_table(table, recreate="always") as batch:
                for column in text_columns:
                    batch.alter_column(
                        column,
                        type_=sa.LargeBinary(16),
                        existing_type=reflected[column]["type"],
                        existing_nullable=reflected[column]["nullable"],
                    )
            inspector = sa.inspect(bind)
    finally:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def upgrade() -> None:
    bind = op.get_bind()
    if not _stock_schema_is_present(bind):
        _apply_stock_post_z5()
    _convert_custom_ids()


def downgrade() -> None:
    # The deployment rollback points at the previous immutable application and
    # restores the pre-cutover database backup; in-place downgrade is unsafe
    # for the binary identifier conversion and is intentionally unsupported.
    raise RuntimeError("zd1b2c3d4e5f is not safely reversible; restore the backup")
