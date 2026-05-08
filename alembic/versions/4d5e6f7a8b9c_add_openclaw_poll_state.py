"""add openclaw poll state

Revision ID: 4d5e6f7a8b9c
Revises: 2b1f4d8a9c03
Create Date: 2026-05-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4d5e6f7a8b9c"
down_revision: str | Sequence[str] | None = "2b1f4d8a9c03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deep_analyses", sa.Column("poll_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("deep_analyses", sa.Column("next_poll_at", sa.DateTime(), nullable=True))
    op.add_column("deep_analyses", sa.Column("last_polled_at", sa.DateTime(), nullable=True))
    op.create_index(
        "idx_deep_analyses_pending_next_poll",
        "deep_analyses",
        ["next_poll_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("idx_deep_analyses_pending_next_poll", table_name="deep_analyses")
    op.drop_column("deep_analyses", "last_polled_at")
    op.drop_column("deep_analyses", "next_poll_at")
    op.drop_column("deep_analyses", "poll_attempts")
