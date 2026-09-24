"""Add per-task Codex search and generated-audio preferences."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "c91f6a2d7e40"
down_revision = "b4d8e2f6a9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_tasks",
        sa.Column("codex_web_search_mode", sa.String(16), nullable=True),
    )
    op.add_column(
        "scheduled_tasks",
        sa.Column("audio_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "scheduled_tasks",
        sa.Column("audio_voice_profile", sa.String(64), nullable=True),
    )
    op.create_table(
        "generated_response_audio",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("response_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("voice_profile", sa.String(64), nullable=False),
        sa.Column("artifact_key", sa.String(255), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')",
            name="ck_generated_response_audio_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "response_id"),
    )
    op.create_index(
        "ix_generated_response_audio_conversation",
        "generated_response_audio",
        ["workspace_id", "conversation_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generated_response_audio_conversation",
        table_name="generated_response_audio",
    )
    op.drop_table("generated_response_audio")
    op.drop_column("scheduled_tasks", "audio_voice_profile")
    op.drop_column("scheduled_tasks", "audio_enabled")
    op.drop_column("scheduled_tasks", "codex_web_search_mode")
