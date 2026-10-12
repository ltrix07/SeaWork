"""Add channel_accounts: which person stands behind a channel's account."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_channel_accounts"
down_revision: str | None = "0006_deliveries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_profiles.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column("hiring_country_asked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("channel", "external_id", name="uq_channel_accounts_ext"),
    )
    op.create_index("ix_channel_accounts_user", "channel_accounts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_channel_accounts_user", table_name="channel_accounts")
    op.drop_table("channel_accounts")
