"""add model routing fields to conversations

Revision ID: z6a2b3c4d5e6
Revises: z5a2b3c4d5e6
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "z6a2b3c4d5e6"
down_revision: str | None = "x1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("route_approval_enabled", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("omniroute_route_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("permission_mode", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("omniroute_requires_explicit_approval", sa.Boolean(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("omniroute_requires_explicit_approval")
        batch_op.drop_column("permission_mode")
        batch_op.drop_column("omniroute_route_id")
        batch_op.drop_column("route_approval_enabled")
