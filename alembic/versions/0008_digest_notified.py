"""Carousel support: the morning notice, refused cards, one delivery per vacancy and person."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_digest_notified"
down_revision: str | None = "0007_channel_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channel_accounts", sa.Column("digest_notified_at", sa.DateTime(timezone=True)))
    op.add_column(
        "deliveries",
        sa.Column("rejected", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_unique_constraint(
        "uq_deliveries_user_opportunity", "deliveries", ["user_id", "opportunity_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_deliveries_user_opportunity", "deliveries", type_="unique")
    op.drop_column("deliveries", "rejected")
    op.drop_column("channel_accounts", "digest_notified_at")
