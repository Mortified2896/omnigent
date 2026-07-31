"""repair missing conversations columns, checks, and indexes

The live ``chat.db`` is missing 13 columns on ``conversations``
(``runner_id``, ``host_id``, ``reasoning_effort``, ``model_override``,
``cost_control_mode_override``, ``harness_override``, ``sub_agent_name``,
``external_session_id``, ``session_state``, ``session_usage``,
``terminal_launch_args``, ``workspace``, ``git_branch``) plus the
``kind`` column and its check + index. SQLite has no ``ALTER TABLE ...
ADD CONSTRAINT``, so each column-add goes through a batch rebuild and
the existing-row default comes from ``server_default`` for nullable
columns.

These columns were each added in earlier migrations
(``c9d3a1f2e4b5_add_runner_id_to_conversations``,
``a7b3c9d1e5f2_add_hosts_table_and_host_id``,
``d7e8f9a0b1c2_add_cost_control_mode_override_to_conversations``,
``c1d2e3f4a5b6_add_model_override_to_conversations``,
``f1a2b3c4d5e6_add_sub_agent_name_to_conversations``,
``f8e1a23d6c47_add_external_session_id_to_conversations``,
``f2a3b4c5d6e7_add_session_state_to_conversations``,
``b2c3d4e5f6a7_add_session_usage_to_conversations``,
``a7f3c2d18e94_add_terminal_launch_args_to_conversations``,
``b8c4f2e7a9d1_add_workspace_to_conversations``,
``caf81af91d9e_add_git_branch_to_conversations``,
``u1a2b3c4d5e6_enums_varchar_to_smallint`` for the ``kind`` swap).

Inspecting the live DB shows none of those migrations actually
landed the column on the table — the alembic_version pointer sits at
the current head but the schema reflects an earlier snapshot. The
likely cause is a hand-rebuilt ``chat.db`` from a partial dump that
omitted the later migrations. The DB has 144 live ``conversations``
rows; nullifying them would break the eval harness. Each repair step
is a no-op when the column already exists, so this migration is safe
to re-run on databases that already received the original migrations.

Revision ID: zg1a2b3c4d5e
Revises: zf1a2b3c4d5e
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "zg1a2b3c4d5e"
down_revision: str | None = "zf1a2b3c4d5e"
branch_labels = None
depends_on = None


# (column_name, column_type, nullable, server_default) — keep the
# column shapes aligned with the per-column migrations listed above
# so the live DB ends up byte-equivalent to a fresh ``alembic upgrade
# head`` from scratch.
_REPAIR_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, bool, str | None], ...] = (
    ("kind", sa.SmallInteger(), False, "1"),
    ("runner_id", sa.String(length=64), True, None),
    ("host_id", sa.String(length=64), True, None),
    ("reasoning_effort", sa.String(length=32), True, None),
    ("model_override", sa.String(length=128), True, None),
    ("cost_control_mode_override", sa.String(length=8), True, None),
    ("harness_override", sa.String(length=64), True, None),
    ("sub_agent_name", sa.String(length=128), True, None),
    ("external_session_id", sa.String(length=128), True, None),
    ("session_state", sa.Text(), True, None),
    ("session_usage", sa.Text(), True, None),
    ("terminal_launch_args", sa.Text(), True, None),
    ("workspace", sa.String(length=2048), True, None),
    ("git_branch", sa.String(length=255), True, None),
)

_CHECK_CONSTRAINTS: tuple[tuple[str, str], ...] = (("ck_conversations_kind", "kind IN (1, 2)"),)

_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_conversations_kind", ["workspace_id", "kind", "id"]),
)


def _column_names(table: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns(table)}


def _check_constraint_names(table: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_check_constraints(table)}


def _index_names(table: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    """Repair the conversations table to match the model."""
    existing_cols = _column_names("conversations")

    for name, ctype, nullable, server_default in _REPAIR_COLUMNS:
        if name in existing_cols:
            continue
        kwargs: dict[str, object] = {"nullable": nullable}
        if server_default is not None:
            kwargs["server_default"] = server_default
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.add_column(sa.Column(name, ctype, **kwargs))

    existing_checks = _check_constraint_names("conversations")
    for cname, clause in _CHECK_CONSTRAINTS:
        if cname in existing_checks:
            continue
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.create_check_constraint(cname, clause)

    existing_indexes = _index_names("conversations")
    for iname, cols in _INDEXES:
        if iname in existing_indexes:
            continue
        op.create_index(iname, "conversations", cols)


def downgrade() -> None:
    """Reverse the repairs (forward-only on production).

    In-place downgrade is forbidden. The repair migration is a
    one-way idempotent patch: dropping the columns and the ``kind``
    check constraint on a live ``conversations`` table would lose
    data on the 144 production rows that already carry those
    columns. Restore the pre-cutover backup instead.
    """
    raise RuntimeError(
        "zg1a2b3c4d5e is not safely reversible; restore the pre-cutover backup"
    )
