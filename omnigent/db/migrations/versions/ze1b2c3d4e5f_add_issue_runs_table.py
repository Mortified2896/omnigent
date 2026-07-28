"""add issue_runs + issue_run_events tables for Oversight Autopilot v1

Issue #18: durable issue-run persistence + atomic leasing. Two new
tables back the lease-based claim lock the scheduler and the runner
share:

- ``issue_runs`` — one row per (workspace_id, repository,
  issue_number) Autopilot run. The lease columns are the atomic
  claim lock; the store is the only writer.
- ``issue_run_events`` — append-only event log every state
  transition + external observation. Replaying the log in
  ``sequence`` order reconstructs the run's last-known state.

Both tables land in this revision because they ship as one unit; a
split would leave a half-built :class:`IssueRunStore` with no
events table to write to. The migration is forward-only — there is
no safe downgrade (the same constraint as ``zd1b2c3d4e5f``); the
store defends against an in-place down-migration attempt.

Revision ID: ze1b2c3d4e5f
Revises: zd1b2c3d4e5f
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ze1b2c3d4e5f"
down_revision = "zd1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create ``issue_runs`` + ``issue_run_events`` with full index set.

    The batch / recreate dance is unnecessary on SQLite at the
    sizes these tables start at — both are empty on a fresh
    deployment and stay small (one row per active issue, ~1k
    events per run) on a long-running deployment. Postgres /
    MySQL take the plain CREATE TABLE path the dialect prefers.
    """
    op.create_table(
        "issue_runs",
        sa.Column(
            "workspace_id", sa.BigInteger(), primary_key=True, nullable=False, server_default="0"
        ),
        sa.Column("id", sa.LargeBinary(16), primary_key=True),
        sa.Column("repository", sa.String(length=255), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        # Lifecycle state as a stable int code (see
        # ``omnigent.db.enum_codecs.ISSUE_RUN_STATE``).
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("lease_owner", sa.LargeBinary(16), nullable=True),
        sa.Column("lease_acquired_at", sa.BigInteger(), nullable=True),
        sa.Column("lease_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("parent_session_id", sa.LargeBinary(16), nullable=True),
        sa.Column("worker_session_id", sa.LargeBinary(16), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("worktree", sa.String(length=2048), nullable=True),
        sa.Column("head_sha", sa.String(length=64), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column(
            "review_iteration", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "retry_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("issue_number > 0", name="ck_issue_runs_issue_number_positive"),
    )
    # Recovery sweep + scheduler queries each need their own index.
    # ``(workspace_id, repository, issue_number)`` is the natural key
    # the scheduler uses to find the current run for a given issue.
    op.create_index(
        "ix_issue_runs_repo_issue",
        "issue_runs",
        ["workspace_id", "repository", "issue_number"],
    )
    # ``(state, lease_expires_at)`` is the recovery sweep's primary
    # scan: WHERE state IN (non-terminal) AND lease_expires_at IS NOT NULL
    # AND lease_expires_at < now ORDER BY lease_expires_at, id.
    op.create_index(
        "ix_issue_runs_state_lease",
        "issue_runs",
        ["state", "lease_expires_at", "id"],
    )
    # ``(workspace_id, repository, state, updated_at)`` powers the
    # "list active runs across a repo" listing the scheduler uses
    # to assert the V1 invariant of at most one active run globally.
    op.create_index(
        "ix_issue_runs_repo_state_updated",
        "issue_runs",
        ["workspace_id", "repository", "state", "updated_at"],
    )

    op.create_table(
        "issue_run_events",
        sa.Column(
            "workspace_id", sa.BigInteger(), primary_key=True, nullable=False, server_default="0"
        ),
        sa.Column("id", sa.LargeBinary(16), primary_key=True),
        sa.Column("run_id", sa.LargeBinary(16), nullable=False),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=True),
        sa.Column("payload", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    # Replay path: per-run events in sequence order. ``run_id`` is
    # not a foreign key (Rule R032) — the store handles cascade via
    # ``OMNIGENT_ISSUE_RUN_EVENT_RETAIN`` so audits survive a deleted
    # run; the index still speeds up the per-run replay the recovery
    # sweep uses.
    op.create_index(
        "ix_issue_run_events_run_sequence",
        "issue_run_events",
        ["workspace_id", "run_id", "sequence"],
    )
    op.create_index(
        "ix_issue_run_events_kind_created",
        "issue_run_events",
        ["workspace_id", "kind", "created_at"],
    )


def downgrade() -> None:
    """Forward-only migration.

    Mirrors ``zd1b2c3d4e5f``: the deployment rollback path keeps a
    pre-cutover database backup and restores from it; an in-place
    downgrade would silently drop the issue_run_events rows a
    re-deployed schema expects.
    """
    raise RuntimeError(
        "ze1b2c3d4e5f is not safely reversible; restore the pre-cutover backup"
    )