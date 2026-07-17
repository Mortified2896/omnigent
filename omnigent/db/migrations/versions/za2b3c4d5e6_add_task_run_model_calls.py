"""add per-model-call execution provenance

Revision ID: za2b3c4d5e6
Revises: z9a2b3c4d5e6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "za2b3c4d5e6"
down_revision = "z9a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_run_model_calls",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("task_run_id", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("opencode_session_id", sa.String(128)),
        sa.Column("requested_provider", sa.String(128), nullable=False),
        sa.Column("requested_model", sa.String(128), nullable=False),
        sa.Column("requested_reasoning", sa.String(32)),
        sa.Column("effective_reasoning", sa.String(32)),
        sa.Column("stream", sa.Boolean()),
        sa.Column("selected_provider", sa.String(128)),
        sa.Column("selected_model", sa.String(128)),
        sa.Column("omniroute_request_id", sa.String(128)),
        sa.Column("omniroute_decision_id", sa.String(128)),
        sa.Column("fallback_used", sa.Boolean()),
        sa.Column("selection_strategy", sa.String(64)),
        sa.Column("billing_class", sa.String(32)),
        sa.Column("provenance_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("request_status", sa.String(32), nullable=False, server_default="in_progress"),
        sa.Column("http_status", sa.Integer()),
        sa.Column("failure_stage", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("first_response_at", sa.BigInteger()),
        sa.Column("finished_at", sa.BigInteger()),
        sa.Column("duration_ms", sa.BigInteger()),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id", "correlation_id", name="uq_task_run_model_calls_correlation"
        ),
        sa.UniqueConstraint(
            "workspace_id", "task_run_id", "ordinal", name="uq_task_run_model_calls_ordinal"
        ),
        sa.UniqueConstraint(
            "workspace_id", "omniroute_request_id", name="uq_task_run_model_calls_request"
        ),
    )
    op.create_index(
        "ix_task_run_model_calls_run_ordinal",
        "task_run_model_calls",
        ["workspace_id", "task_run_id", "ordinal"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_run_model_calls_run_ordinal", table_name="task_run_model_calls")
    op.drop_table("task_run_model_calls")
