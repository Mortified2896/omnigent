"""Add response feedback to conversation persistence."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "f8a9b0c1d2e3"
down_revision = "ge1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "response_feedback",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("response_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "response_id", "user_id"),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_response_feedback_rating"),
    )


def downgrade() -> None:
    op.drop_table("response_feedback")
