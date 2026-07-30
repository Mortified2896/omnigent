"""deliberately broken migration for the rollback acceptance test

Issue #38 acceptance: this migration always raises during ``upgrade``
so the updater's migration-rehearsal phase fails, the controller
records the failure, and ``rollback_release.sh`` restores the
previous release. We never expect this migration to be applied —
it is removed before merge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ze2b3c4d5e6f"
down_revision = "ze1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Deliberately fail so the canary/rehearsal phases abort."""
    raise RuntimeError(
        "deliberately broken candidate (rollback acceptance test): "
        "this migration is never meant to run"
    )


def downgrade() -> None:
    raise RuntimeError("ze2b3c4d5e6f is not safely reversible")
