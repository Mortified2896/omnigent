"""add route_approval_enabled to conversations

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-07-08 00:00:00.000000

Adds the per-session route-approval toggle (mirrors the cost-control
switch shape):

- ``route_approval_enabled``: nullable Boolean — ``True`` gates
  message execution behind the route proposal approval card,
  ``False`` / NULL leaves the session running without the gate.

Set via ``POST /v1/sessions`` / ``PATCH /v1/sessions/{id}`` (parallel
to ``cost_control_mode_override``) and read by the server-side
execution gate before forwarding a user message to the runner.

Combined server-side with ``OMNIGENT_ROUTE_APPROVAL_GATE``: the
global env gate must also be enabled for the per-session toggle to
take effect.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o1a2b3c4d5e6"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column("route_approval_enabled", sa.Boolean(), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("route_approval_enabled")