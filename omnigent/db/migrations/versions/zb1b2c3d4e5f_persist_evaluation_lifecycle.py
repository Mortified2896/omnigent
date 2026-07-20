"""persist durable MiniMax-M3 evaluation lifecycle

A TaskEvaluation now means a real, valid automated judgment. Historical
``inconclusive`` rows are preserved unless their reasoning starts with the
exact implementation-generated ``Automated evaluation unavailable:`` prefix.
Only those reliably identifiable synthetic failure rows are removed; their
bounded reason is copied to the owning run as ``deferred`` or ``failed``.
Genuine historical inconclusive judgments are retained and marked completed.

Revision ID: zb1b2c3d4e5f
Revises: za1b2c3d4e5f
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "zb1b2c3d4e5f"
down_revision = "za1b2c3d4e5f"
branch_labels = None
depends_on = None

_FAKE_PREFIX = "Automated evaluation unavailable:"
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "rate limit",
    "cooldown",
    "quota",
    "all_accounts_inactive",
    "http 429",
    "http 502",
    "http 503",
    "http 504",
    "connection",
)


def upgrade() -> None:
    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(
            sa.Column(
                "evaluation_attempt_count", sa.BigInteger(), nullable=False, server_default="0"
            )
        )
        batch.add_column(sa.Column("evaluation_last_attempt_at", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("evaluation_next_retry_at", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("evaluation_error_kind", sa.String(64), nullable=True))
        batch.add_column(sa.Column("evaluation_error_code", sa.String(128), nullable=True))
        batch.add_column(sa.Column("evaluation_error_message", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "evaluation_requested_model",
                sa.String(128),
                nullable=True,
                server_default="minimax/MiniMax-M3",
            )
        )
        batch.create_check_constraint(
            "ck_task_runs_evaluation_status",
            "evaluation_status IN "
            "('not_requested','pending','completed','deferred','skipped','failed')",
        )
        batch.create_check_constraint(
            "ck_task_runs_evaluation_attempt_count", "evaluation_attempt_count >= 0"
        )
        batch.create_index(
            "ix_task_runs_evaluation_due",
            ["workspace_id", "evaluation_status", "evaluation_next_retry_at", "id"],
        )

    with op.batch_alter_table("task_evaluations") as batch:
        batch.add_column(sa.Column("evaluator_fallback_used", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("evaluator_decision_id", sa.String(128), nullable=True))

    connection = op.get_bind()
    fake_rows = connection.execute(
        sa.text(
            "SELECT workspace_id, id, task_run_id, reasoning, created_at "
            "FROM task_evaluations "
            "WHERE verdict = 'inconclusive' AND reasoning LIKE :prefix"
        ),
        {"prefix": _FAKE_PREFIX + "%"},
    ).mappings()
    for row in fake_rows:
        reason = str(row["reasoning"] or _FAKE_PREFIX)[:1000]
        lowered = reason.lower()
        transient = any(marker in lowered for marker in _TRANSIENT_MARKERS)
        connection.execute(
            sa.text(
                "UPDATE task_runs SET evaluation_status = :status, "
                "evaluation_attempt_count = CASE WHEN evaluation_attempt_count < 1 THEN 1 "
                "ELSE evaluation_attempt_count END, "
                "evaluation_last_attempt_at = COALESCE(evaluation_last_attempt_at, :attempted), "
                "evaluation_error_kind = :kind, evaluation_error_code = :code, "
                "evaluation_error_message = :message, evaluation_finished_at = :finished, "
                "updated_at = CASE WHEN updated_at < :attempted "
                "THEN :attempted ELSE updated_at END "
                "WHERE workspace_id = :workspace_id AND id = :task_run_id"
            ),
            {
                "status": "deferred" if transient else "failed",
                "attempted": int(row["created_at"]),
                "kind": "availability" if transient else "historical_evaluator_failure",
                "code": "historical_transient" if transient else "historical_failure",
                "message": reason,
                "finished": None if transient else int(row["created_at"]),
                "workspace_id": int(row["workspace_id"]),
                "task_run_id": str(row["task_run_id"]),
            },
        )
        connection.execute(
            sa.text(
                "DELETE FROM task_evaluations WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": int(row["workspace_id"]), "id": str(row["id"])},
        )

    # Every remaining row is a judgment that cannot reliably be identified as
    # synthetic. Preserve it and restore the completed-row invariant.
    connection.execute(
        sa.text(
            "UPDATE task_runs SET evaluation_status = 'completed', "
            "evaluation_finished_at = COALESCE(evaluation_finished_at, "
            "(SELECT MAX(e.created_at) FROM task_evaluations e "
            "WHERE e.workspace_id = task_runs.workspace_id AND e.task_run_id = task_runs.id)) "
            "WHERE EXISTS (SELECT 1 FROM task_evaluations e WHERE "
            "e.workspace_id = task_runs.workspace_id AND e.task_run_id = task_runs.id)"
        )
    )

    with op.batch_alter_table("task_evaluations") as batch:
        batch.create_unique_constraint("uq_task_evaluations_run", ["workspace_id", "task_run_id"])


def downgrade() -> None:
    with op.batch_alter_table("task_evaluations") as batch:
        batch.drop_constraint("uq_task_evaluations_run", type_="unique")
        batch.drop_column("evaluator_decision_id")
        batch.drop_column("evaluator_fallback_used")

    # Older code does not understand deferred. It can safely expose those rows
    # as failed while retaining their diagnostic data until the columns drop.
    op.execute(
        "UPDATE task_runs SET evaluation_status = 'failed' WHERE evaluation_status = 'deferred'"
    )
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_index("ix_task_runs_evaluation_due")
        batch.drop_constraint("ck_task_runs_evaluation_attempt_count", type_="check")
        batch.drop_constraint("ck_task_runs_evaluation_status", type_="check")
        for name in (
            "evaluation_requested_model",
            "evaluation_error_message",
            "evaluation_error_code",
            "evaluation_error_kind",
            "evaluation_next_retry_at",
            "evaluation_last_attempt_at",
            "evaluation_attempt_count",
        ):
            batch.drop_column(name)
