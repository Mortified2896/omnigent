"""Add the model advisor record store.

One table backing advisor preferences and durable advisor rounds
(``omnigent.model_advisor_repository``). Rows are keyed by a digest scope
binding owner + host + record kind, so the table carries no foreign keys
(DBSPEC R032) and no workspace column of its own.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "b4d8e2f6a9c1"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_advisor_records",
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.PrimaryKeyConstraint("scope_key"),
    )


def downgrade() -> None:
    op.drop_table("model_advisor_records")
