"""add durable routing proposals and decisions

Revision ID: za1b2c3d4e5f
Revises: z9a2b3c4d5e6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "za1b2c3d4e5f"
down_revision = "z9a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("routing_selection_source", sa.String(32), nullable=True))
        batch.create_check_constraint(
            "ck_conversations_routing_selection_source",
            "routing_selection_source IS NULL OR "
            "routing_selection_source IN ('manual','route_approval')",
        )

    op.create_table(
        "routing_proposals",
        sa.Column(
            "workspace_id", sa.BigInteger(), primary_key=True, nullable=False, server_default="0"
        ),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("elicitation_id", sa.String(64), nullable=False),
        sa.Column("user_message_sha256", sa.String(64), nullable=False),
        sa.Column("user_message_excerpt", sa.Text(), nullable=False),
        sa.Column("user_message_chars", sa.BigInteger(), nullable=False),
        sa.Column("content_types_json", sa.Text(), nullable=False),
        sa.Column("original_harness", sa.String(64), nullable=True),
        sa.Column("original_provider", sa.String(128), nullable=True),
        sa.Column("original_model", sa.String(128), nullable=True),
        sa.Column("original_route_id", sa.String(64), nullable=False),
        sa.Column("original_reasoning_effort", sa.String(32), nullable=True),
        sa.Column("original_permission_mode", sa.String(64), nullable=True),
        sa.Column("requires_explicit_approval", sa.Boolean(), nullable=False),
        sa.Column("evaluator_route_id", sa.String(64), nullable=True),
        sa.Column("evaluator_provider", sa.String(128), nullable=True),
        sa.Column("evaluator_model", sa.String(128), nullable=True),
        sa.Column("evaluator_billing_class", sa.String(32), nullable=True),
        sa.Column("evaluator_fallback_used", sa.Boolean(), nullable=True),
        sa.Column("evaluator_decision_id", sa.String(128), nullable=True),
        sa.Column("evaluator_selection_strategy", sa.String(64), nullable=True),
        sa.Column("proposal_payload_excerpt", sa.Text(), nullable=False),
        sa.Column("proposal_payload_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversations.workspace_id", "conversations.id"],
            name="fk_routing_proposals_conversation",
        ),
        sa.UniqueConstraint(
            "workspace_id", "elicitation_id", name="uq_routing_proposals_elicitation"
        ),
    )
    op.create_index(
        "ix_routing_proposals_conversation_created",
        "routing_proposals",
        ["workspace_id", "conversation_id", "created_at", "id"],
    )

    op.create_table(
        "routing_decisions",
        sa.Column(
            "workspace_id", sa.BigInteger(), primary_key=True, nullable=False, server_default="0"
        ),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("proposal_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("decision_request_sha256", sa.String(64), nullable=False),
        sa.Column("original_harness", sa.String(64), nullable=True),
        sa.Column("original_provider", sa.String(128), nullable=True),
        sa.Column("original_model", sa.String(128), nullable=True),
        sa.Column("original_route_id", sa.String(64), nullable=False),
        sa.Column("original_reasoning_effort", sa.String(32), nullable=True),
        sa.Column("original_permission_mode", sa.String(64), nullable=True),
        sa.Column("final_harness", sa.String(64), nullable=True),
        sa.Column("final_provider", sa.String(128), nullable=True),
        sa.Column("final_model", sa.String(128), nullable=True),
        sa.Column("final_route_id", sa.String(64), nullable=True),
        sa.Column("final_reasoning_effort", sa.String(32), nullable=True),
        sa.Column("final_permission_mode", sa.String(64), nullable=True),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decision_payload_excerpt", sa.Text(), nullable=False),
        sa.Column("decision_payload_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "action IN ('approved','changed','declined')", name="ck_routing_decisions_action"
        ),
        sa.CheckConstraint(
            "action = 'declined' OR final_route_id IS NOT NULL OR final_model IS NOT NULL",
            name="ck_routing_decisions_final_route",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["routing_proposals.workspace_id", "routing_proposals.id"],
            name="fk_routing_decisions_proposal",
        ),
        sa.UniqueConstraint("workspace_id", "proposal_id", name="uq_routing_decisions_proposal"),
    )
    op.create_index(
        "ix_routing_decisions_created",
        "routing_decisions",
        ["workspace_id", "created_at", "id"],
    )

    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(sa.Column("routing_proposal_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("routing_decision_id", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_task_runs_routing_proposal",
            "routing_proposals",
            ["workspace_id", "routing_proposal_id"],
            ["workspace_id", "id"],
        )
        batch.create_foreign_key(
            "fk_task_runs_routing_decision",
            "routing_decisions",
            ["workspace_id", "routing_decision_id"],
            ["workspace_id", "id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_constraint("fk_task_runs_routing_decision", type_="foreignkey")
        batch.drop_constraint("fk_task_runs_routing_proposal", type_="foreignkey")
        batch.drop_column("routing_decision_id")
        batch.drop_column("routing_proposal_id")
    op.drop_index("ix_routing_decisions_created", table_name="routing_decisions")
    op.drop_table("routing_decisions")
    op.drop_index("ix_routing_proposals_conversation_created", table_name="routing_proposals")
    op.drop_table("routing_proposals")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_constraint("ck_conversations_routing_selection_source", type_="check")
        batch.drop_column("routing_selection_source")
