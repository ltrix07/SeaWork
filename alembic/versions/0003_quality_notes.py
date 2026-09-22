"""Add quality_notes: facts kept about a record without rejecting it."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_quality_notes"
down_revision: str | None = "0002_llm_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default is set for the backfill and then dropped: existing rows need a
    # value, new rows get theirs from the application, and leaving the default in
    # place would quietly hide a writer that forgot the column.
    op.add_column(
        "opportunities",
        sa.Column(
            "quality_notes",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("opportunities", "quality_notes", server_default=None)


def downgrade() -> None:
    op.drop_column("opportunities", "quality_notes")
