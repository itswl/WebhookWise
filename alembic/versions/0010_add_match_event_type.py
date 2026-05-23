"""add match_event_type to forward_rules

Revision ID: 0010_add_match_event_type
Revises: 0009_add_dedup_key
Create Date: 2026-05-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_add_match_event_type"
down_revision: str | Sequence[str] | None = "0009_add_dedup_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [c["name"] for c in inspector.get_columns(table)]
    return column in cols


def upgrade() -> None:
    if not _column_exists("forward_rules", "match_event_type"):
        op.add_column(
            "forward_rules",
            sa.Column("match_event_type", sa.String(length=200), server_default="", nullable=False),
        )


def downgrade() -> None:
    if _column_exists("forward_rules", "match_event_type"):
        op.drop_column("forward_rules", "match_event_type")
