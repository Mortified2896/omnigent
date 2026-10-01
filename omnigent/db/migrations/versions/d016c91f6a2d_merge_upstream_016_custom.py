"""Join upstream 0.16 and the deployed fork migration histories.

Both branches retain their original revisions and execute before this join.
No historical migrations or application data are changed by the join itself.
"""

from collections.abc import Sequence

revision: str = "d016c91f6a2d"
down_revision: tuple[str, str] = ("ll1a2b3c4d5e", "c91f6a2d7e40")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
