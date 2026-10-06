"""Join upstream 0.17 and the deployed fork's migration history.

The upstream additive agent index and all fork metadata migrations remain
unchanged. This join changes no application data.
"""

from collections.abc import Sequence

revision: str = "d017a6b2c940"
down_revision: tuple[str, str] = ("d016c91f6a2d", "mm1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
