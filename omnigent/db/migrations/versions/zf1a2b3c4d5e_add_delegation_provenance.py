"""add delegation_provenance JSON column to conversations

Issue #56: the OpenCode selector silently resolved to Verity/claude-sdk
in production. Add a durable, JSON-encoded ``conversations.delegation_provenance``
column capturing the requested selector, resolved agent identity, harness,
OmniRoute route, provider/model, reasoning effort, fallback flag,
workspace/worktree, parent/child conversation ids, and the decision or
rejection reason. Single source of truth for post-incident forensics
("did this session ever reach native OpenCode, or did it fall through?").

The column is nullable so legacy rows survive untouched; the resolver
populates it on create for any session that goes through
``omnigent.agent_selector.resolve_delegate_agent`` and on every
delegation revalidation before child launch. New sessions that do
NOT go through the canonical resolver (e.g. legacy users of
``POST /v1/sessions {agent_id, harness_override: "opencode-native"}``
without ``agent_selector``) leave the column NULL — that's fine; the
resolver is an additive gate, not a breaking rename.

Revision ID: zf1a2b3c4d5e
Revises: ze1b2b3c4d5e
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "zf1a2b3c4d5e"
down_revision: str | None = "ze1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the JSON ``delegation_provenance`` column to ``conversations``.

    Nullable so legacy rows and rows created via non-canonical paths
    (e.g. direct ``POST /v1/sessions {agent_id: ...}`` without
    ``agent_selector``) survive untouched. New delegations fill the
    column with the resolver's structured output via
    :func:`omnigent.agent_selector.resolve_delegate_agent`.
    """
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "delegation_provenance",
                sa.Text(),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Drop the ``delegation_provenance`` column.

    Forward-only rationale mirrors the rest of the durable-provenance
    migrations (``zd1b2c3d4e5f``): downgrading erases the resolver's
    audit trail, and a half-built deployment would still emit the
    column from the new code paths. Operators that must roll back
    should also pin the pre-fix wheel.
    """
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("delegation_provenance")
