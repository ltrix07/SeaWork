"""Create ingestion tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_ingestion"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
    )
    op.create_index(
        "uq_raw_items_source_external_hash",
        "raw_items",
        ["source_id", "external_id", "content_hash"],
        unique=True,
    )
    op.create_table(
        "opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("raw_item_id", sa.Integer(), sa.ForeignKey("raw_items.id"), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("source_trust", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("employer", sa.Text()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("type_confidence", sa.Float(), nullable=False),
        sa.Column("enriched", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("quality_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "uq_opportunities_source_external",
        "opportunities",
        ["source_id", "external_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("opportunities")
    op.drop_table("raw_items")
