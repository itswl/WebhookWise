"""add dedup_key to webhook_events

Revision ID: 0009_add_dedup_key
Revises: 0008_create_suppressed_records
Create Date: 2026-05-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_add_dedup_key"
down_revision: str | Sequence[str] | None = "0008_create_suppressed_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [c["name"] for c in inspector.get_columns(table)]
    return column in cols


def upgrade() -> None:
    if not _column_exists("webhook_events", "dedup_key"):
        op.add_column("webhook_events", sa.Column("dedup_key", sa.String(length=64), nullable=True))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_dedup_key_timestamp ON webhook_events (dedup_key, timestamp)"))
    op.execute(sa.text("UPDATE webhook_events SET dedup_key = alert_hash WHERE dedup_key IS NULL"))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_dedup_key_timestamp"))
    if _column_exists("webhook_events", "dedup_key"):
        op.drop_column("webhook_events", "dedup_key")
