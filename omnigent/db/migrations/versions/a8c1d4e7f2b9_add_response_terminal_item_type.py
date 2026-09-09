"""Add the durable response-terminal conversation item type.

Revision ID: a8c1d4e7f2b9
Revises: f7a8b9c0d1e2
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a8c1d4e7f2b9"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)"
_NEW = "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)"


def _replace_type_check(expression: str) -> None:
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.drop_constraint("ck_conversation_items_type", type_="check")
        batch_op.create_check_constraint("ck_conversation_items_type", expression)


def upgrade() -> None:
    """Allow response_terminal rows."""
    _replace_type_check(_NEW)


def downgrade() -> None:
    """Restore the old item vocabulary after response-terminal rows are removed."""
    _replace_type_check(_OLD)
