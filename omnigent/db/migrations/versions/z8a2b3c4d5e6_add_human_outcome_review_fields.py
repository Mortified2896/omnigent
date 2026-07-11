"""add human approval semantics to task reviews"""
from alembic import op
import sqlalchemy as sa

revision = "z8a2b3c4d5e6"
down_revision = "z7a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_reviews", sa.Column("review_action", sa.String(16), nullable=True))
    op.add_column("task_reviews", sa.Column("learning_eligible", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("task_reviews", sa.Column("route_fit", sa.String(32), nullable=True))
    op.add_column("task_reviews", sa.Column("failure_attribution", sa.String(32), nullable=True))
    op.add_column("task_reviews", sa.Column("preferred_route_id", sa.String(64), nullable=True))
    op.add_column("task_reviews", sa.Column("preferred_reasoning_effort", sa.String(16), nullable=True))
    op.add_column("task_reviews", sa.Column("source_evaluation_id", sa.String(64), nullable=True))
    op.add_column("task_reviews", sa.Column("review_schema_version", sa.SmallInteger(), nullable=False, server_default="1"))
    # SQLite cannot ALTER constraints; the application validates these closed
    # vocabularies and the batch operation keeps the same constraints on SQLite.
    with op.batch_alter_table("task_reviews") as batch:
        batch.create_check_constraint("ck_task_reviews_action", "review_action IS NULL OR review_action IN ('accepted','adjusted','declined')")
        batch.create_check_constraint("ck_task_reviews_learning", "learning_eligible = false OR review_action IN ('accepted','adjusted')")
        batch.create_check_constraint("ck_task_reviews_route_fit", "route_fit IS NULL OR route_fit IN ('appropriate','too_weak','overkill','wrong_capability','unsure')")
        batch.create_check_constraint("ck_task_reviews_failure_attribution", "failure_attribution IS NULL OR failure_attribution IN ('router','model','harness','environment','permissions','task_definition','external_service','unknown')")


def downgrade() -> None:
    with op.batch_alter_table("task_reviews") as batch:
        for name in ("ck_task_reviews_failure_attribution", "ck_task_reviews_route_fit", "ck_task_reviews_learning", "ck_task_reviews_action"):
            batch.drop_constraint(name, type_="check")
    for name in ("review_schema_version", "source_evaluation_id", "preferred_reasoning_effort", "preferred_route_id", "failure_attribution", "route_fit", "learning_eligible", "review_action"):
        op.drop_column("task_reviews", name)
