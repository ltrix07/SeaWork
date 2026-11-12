"""Add deliveries: what the Recommendation Engine has already sent to whom."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_deliveries"
down_revision: str | None = "0005_opportunity_closed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_profiles.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column(
            "similar_ids", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False
        ),
        sa.Column("score_value", sa.Float()),
        sa.Column("comparable_factors", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deliveries_user", "deliveries", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_deliveries_user", table_name="deliveries")
    op.drop_table("deliveries")
