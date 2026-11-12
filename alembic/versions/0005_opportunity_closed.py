"""Add closed_at: when a vacancy disappeared from its source."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_opportunity_closed"
down_revision: str | None = "0004_user_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("opportunities", sa.Column("closed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("opportunities", "closed_at")
