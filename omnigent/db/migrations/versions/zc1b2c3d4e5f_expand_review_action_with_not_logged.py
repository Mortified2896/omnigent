"""expand review_action to include 'not_logged'

The ``Don't log`` action replaces ``Decline`` for outcome reviews whose result
must not influence routing learning, outcome success rates, quality averages,
or model/route comparisons. It must also not produce a new review-driven
Langfuse export for the exclusion itself. A minimal review row is still
written for integrity, debugging, cost accounting, and audit.

Adds ``not_logged`` to the ``review_action`` CHECK constraint. Existing
``declined`` rows are preserved as-is — historical rows remain readable.

Revision ID: zc1b2c3d4e5f
Revises: zb1b2c3d4e5f
"""

from __future__ import annotations

from alembic import op

revision = "zc1b2c3d4e5f"
down_revision = "zb1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_reviews") as batch:
        batch.drop_constraint("ck_task_reviews_action", type_="check")
        batch.create_check_constraint(
            "ck_task_reviews_action",
            "review_action IS NULL OR review_action IN "
            "('accepted','adjusted','declined','not_logged')",
        )


def downgrade() -> None:
    with op.batch_alter_table("task_reviews") as batch:
        batch.drop_constraint("ck_task_reviews_action", type_="check")
        batch.create_check_constraint(
            "ck_task_reviews_action",
            "review_action IS NULL OR review_action IN ('accepted','adjusted','declined')",
        )
