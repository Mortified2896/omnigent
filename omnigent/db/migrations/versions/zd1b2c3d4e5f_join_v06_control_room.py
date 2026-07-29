"""Join the v0.6 control-room lineage with the canonical head.

This is the single deployment head for fork/main. Production's database
revision is ``zd1b2c3d4e5f``; before this migration existed in fork/main
the deployment canary could not reach that revision and refused to start.

Fork/main's existing lineage ends at ``zc1b2c3d4e5f`` and already includes
the v0.6 stock schema changes (the schedules table, conversations.id
as a 16-byte uuid, conversations.session_overrides, and the
``routing_proposals`` / ``routing_decisions`` / ``task_runs`` /
``task_evaluations`` / ``task_reviews`` / ``langfuse_sync_outbox`` tables
with all task-outcome audit columns). What the fork/main lineage does
NOT do is persist the opaque identifier columns as raw 16-byte binary;
they are kept as ``String(64)``. Production stores the same identifiers
as ``LargeBinary(16)`` so they survive cross-workspace joins on
PostgreSQL / MySQL and round-trip through Databricks' binary uuid
helper unchanged.

This revision upgrades a fork/main ``zc1b2c3d4e5f`` snapshot to
``zd1b2c3d4e5f`` by converting the affected columns from TEXT to
BLOB. The conversion is conditional: production databases that
already reached ``zd1b2c3d4e5f`` via the production-lineage tree
(no-op here — alembic only runs unapplied revisions) are not
re-touched.

Stock post-z5 replay is intentionally omitted. Fork/main's lineage
applies equivalent schema changes inline in earlier ``z6a..z9a``
revisions; replaying the production-lineage stock modules would
require importing 17 modules that are not present in this tree
(and re-running their ``upgrade()`` would fail on the fork/main
schema because the schema markers they expect are absent).

:see: ``omnigent/db/db_models.Uuid16`` for the on-the-wire / on-disk
      representation contract this migration materialises.
:see: ``omnigent/stores/host_store`` and
      ``omnigent/stores/task_outcome_store/sqlalchemy_store`` for the
      callers that bind these identifiers through ``uuid_to_bytes()``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zd1b2c3d4e5f"
down_revision: str | None = "zc1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, columns) — for each control-room audit / outcome table whose
# identifiers must be 16 raw bytes on disk. Tables that don't exist on
# the target DB (e.g. legacy pre-routing forks) are silently skipped.
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


def _id_to_bytes(value: object) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) == 16:
            return raw
        # Some legacy drivers return a hex-encoded ASCII string instead
        # of raw bytes; decode and retry.
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        value = text
    text = str(value).replace("-", "")
    tail = text[-32:]
    if len(tail) == 32:
        try:
            return bytes.fromhex(tail)
        except ValueError:
            pass
    # Deterministic md5 fallback matches ``uuid_to_bytes`` for non-UUID
    # legacy-prefixed identifiers (e.g. ``ag_<hex>``) so cross-store
    # references remain consistent.
    import hashlib

    return hashlib.md5(str(value).encode("utf-8")).digest()


def _convert_custom_ids() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name
    if dialect == "sqlite":
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))
    try:
        for table, columns in _CUSTOM_ID_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            reflected = {c["name"]: c for c in inspector.get_columns(table)}
            target = [c for c in columns if c in reflected]
            text_columns = [
                c
                for c in target
                if not str(reflected[c]["type"]).upper().startswith("BLOB")
                and not str(reflected[c]["type"]).upper().startswith("BINARY")
            ]
            if not text_columns:
                continue
            # Translate values into 16 raw bytes before the column type
            # changes, otherwise referential updates will compare
            # differently-encoded bytes after the type swap.
            selected = ", ".join(f'"{c}"' for c in text_columns)
            rows = bind.execute(sa.text(f'SELECT rowid, {selected} FROM "{table}"')).fetchall()
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
                    sa.text(f'UPDATE "{table}" SET {assignments} WHERE rowid = :__rowid'),
                    {**values, "__rowid": row[0]},
                )
            # SQLite requires a table recreate for type changes; batch
            # alter with recreate="always" is the canonical workaround.
            # Other dialects accept ALTER COLUMN in place.
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
        if dialect == "sqlite":
            op.execute(sa.text("PRAGMA foreign_keys = ON"))


def upgrade() -> None:
    _convert_custom_ids()


def downgrade() -> None:
    # In-place downgrade across dialects would have to reverse the
    # hex/binary conversion deterministically; the production deploy
    # path keeps a pre-cutover backup and restores from it instead.
    # Marking this unsupported keeps operators on the safe path.
    raise RuntimeError("zd1b2c3d4e5f is not safely reversible; restore the pre-cutover backup")
