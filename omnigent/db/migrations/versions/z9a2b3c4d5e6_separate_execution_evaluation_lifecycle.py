"""separate task execution and evaluation lifecycles

Existing rows are preserved: their legacy terminal_status is copied to
execution_status (running stays running; incomplete becomes failed), timing is
copied to execution timing, and evaluation remains not_requested.  The legacy
terminal_status column remains a compatibility projection and is never updated
by evaluator code.

Revision ID: z9a2b3c4d5e6
Revises: z8a2b3c4d5e6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "z9a2b3c4d5e6"
down_revision = "z8a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(
            sa.Column("execution_status", sa.String(32), nullable=False, server_default="running")
        )
        batch.add_column(
            sa.Column(
                "evaluation_status", sa.String(32), nullable=False, server_default="not_requested"
            )
        )
        batch.add_column(sa.Column("execution_started_at", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("execution_finished_at", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("execution_duration_ms", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("evaluation_started_at", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("evaluation_finished_at", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("timeout_type", sa.String(32), nullable=True))
        batch.add_column(sa.Column("last_useful_activity_at", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("actual_provider", sa.String(128), nullable=True))
        batch.add_column(sa.Column("actual_provider_model", sa.String(128), nullable=True))
        batch.add_column(sa.Column("actual_provenance_verified", sa.Boolean(), nullable=True))
    # Single-statement backfill kept verbatim from the original authoring;
    # any rewrite must produce an equivalent UPDATE against an existing
    # SQLite row shape. Ruff's E501 is suppressed here on purpose.
    op.execute(
        "UPDATE task_runs SET "
        "execution_status = CASE terminal_status "
        "WHEN 1 THEN 'running' WHEN 2 THEN 'completed' "
        "WHEN 3 THEN 'failed' WHEN 4 THEN 'cancelled' "
        "ELSE 'failed' END, "
        "execution_started_at = started_at, "
        "execution_finished_at = terminal_at, "
        "execution_duration_ms = duration_ms, "
        "actual_provider = selected_provider, "
        "actual_provider_model = selected_model, "
        "actual_provenance_verified = 0"
    )


def downgrade() -> None:
    with op.batch_alter_table("task_runs") as batch:
        for name in (
            "actual_provenance_verified",
            "actual_provider_model",
            "actual_provider",
            "last_useful_activity_at",
            "timeout_type",
            "evaluation_finished_at",
            "evaluation_started_at",
            "execution_duration_ms",
            "execution_finished_at",
            "execution_started_at",
            "evaluation_status",
            "execution_status",
        ):
            batch.drop_column(name)
